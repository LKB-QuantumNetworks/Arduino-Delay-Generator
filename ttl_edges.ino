/* ============================================================
   6-channel TTL waveform generator (edge-based)
   ------------------------------------------------------------
   Each channel is described by:
     - its level at t = 0 (LOW or HIGH)
     - a list of edges "at time t, go HIGH / LOW"
   so any number of pulses, starting HIGH or LOW, is possible.

   - Resolution: 0.5 us internally, times are sent in us
   - Minimum spacing between two output changes: 20 us
     (closer edges are pushed back to 20 us and a warning is sent)
   - Outputs:   channels 1-6 on pins D2-D7, all switched at once
   - Trigger:   internal period, or external TTL on pin 8
                (t = 0 is 20 us after the external rising edge)
   - Status:    LED on pin 13, ON when at least one output is not
                permanently LOW
   - Control:   text commands over USB serial (9600 baud), sent by
                the PC GUI through the BeagleBone

   Commands (times in microseconds, integers):
     P<us>                       set period (internal mode)
     C<n>,<s>,<t1>:<l1>,<t2>:<l2>,...
                                 channel n (1-6): start level s (0/1),
                                 then edges time:level (level 0/1)
     X<n>                        channel n off (always LOW)
     U                           apply: new waveform starts at the
                                 beginning of the next period
     E1 / E0                     external / internal timing
   Replies: "OK", "OK W<k>" (k edges had to be shifted), "ERR <reason>"
   Arduino -> PC: "M<us>"        measured period in external mode

   How it works: all channels' edges are merged into one time-sorted
   table of output states. Timer1 runs freely at 2 MHz, and its
   compare interrupt is set to fire exactly at the next change, where
   it writes the new state of all 6 outputs in one operation. Gaps
   longer than the 16-bit timer range are split into several steps.
   The table is double-buffered: a new waveform is built in the spare
   table and swapped in at t = 0, so updates never glitch the outputs.
   ============================================================ */

#define N_CH        6
#define MAX_EDGES   10                     // edges per channel (5 pulses)
#define MAX_EV      (N_CH * MAX_EDGES + 1) // output changes per period (+1 for t = 0)
#define LED_PIN     13
#define TICKS_PER_US 2UL                   // Timer1 at 16 MHz / 8 = 2 MHz -> 0.5 us per tick
#define MIN_STEP    40                     // 40 ticks = 20 us: min spacing between changes
#define MAX_STEP    60000U                 // longest single timer step (< 65536)
#define TRIG_DELAY  40                     // 20 us: external edge -> t = 0
#define MIN_PERIOD_US 50UL

// ---------- Channel configuration (set by commands, used by buildTable) ----------
uint8_t  ch_start[N_CH];                   // level at t = 0 (0/1)
uint8_t  ch_n[N_CH];                       // number of edges
uint32_t ch_t[N_CH][MAX_EDGES];            // edge times, in ticks, sorted
uint8_t  ch_l[N_CH][MAX_EDGES];            // edge levels (0/1)
uint32_t cfg_period = 100000UL * TICKS_PER_US; // 100 ms

// ---------- Double-buffered event tables (one active, one being built) ----------
uint32_t ev_t[2][MAX_EV];                  // time of each change, in ticks from t = 0
uint8_t  ev_p[2][MAX_EV];                  // PORTD value (bits 2-7) from that time on
uint8_t  ev_n[2]      = {1, 1};            // number of events (event 0 is always t = 0)
uint32_t ev_period[2] = {100000UL * TICKS_PER_US, 100000UL * TICKS_PER_US};
bool     ev_led[2]    = {false, false};    // LED state for each table

volatile uint8_t  active    = 0;           // table used by the interrupt
volatile bool     pending   = false;       // spare table ready -> swap at next t = 0
volatile bool     swapped   = false;       // tells loop() to update the LED

// ---------- Interrupt state ----------
volatile uint8_t  cur       = 0;           // index of the next event to output
volatile uint32_t wait_left = 0;           // remaining ticks of a long gap
volatile bool     ext_mode  = false;
volatile uint16_t ovf       = 0;           // Timer1 overflows (extends the timer to 32 bits)
volatile bool     have_trig = false;
volatile uint32_t last_trig = 0;
volatile uint32_t measured  = 0;           // last external period, in ticks
volatile bool     new_meas  = false;

// Size of the next timer step for a gap of r ticks (never leaves a remainder < MIN_STEP)
static inline uint16_t chunk(uint32_t r) {
  if (r <= MAX_STEP) return (uint16_t)r;
  if (r - MAX_STEP < MIN_STEP) return (uint16_t)(r / 2);
  return MAX_STEP;
}

// Move the compare point s ticks after the previous one. If the interrupt
// was delayed so much that this point is already past, fire as soon as possible
// instead of waiting a full timer turn (32 ms).
static inline void stepCompare(uint16_t s) {
  OCR1A += s;
  if ((uint16_t)(OCR1A - TCNT1) > s) OCR1A = TCNT1 + 8;
}

/* ------------------------------------------------------------
   Timer1 compare interrupt: fires at each output change.
   ------------------------------------------------------------ */
ISR(TIMER1_COMPA_vect) {
  if (wait_left) {                         // in the middle of a long gap: just wait more
    uint16_t s = chunk(wait_left);
    wait_left -= s;
    stepCompare(s);
    return;
  }
  if (cur == 0 && pending) {               // start of a period: switch to the new table
    active ^= 1;
    pending = false;
    swapped = true;
  }
  uint8_t a = active;
  PORTD = (PORTD & 0x03) | ev_p[a][cur];   // all 6 outputs at once (D0/D1 = serial, untouched)

  uint8_t  nxt = cur + 1;
  uint32_t delta;
  if (nxt < ev_n[a]) {
    delta = ev_t[a][nxt] - ev_t[a][cur];
  } else {                                 // last change of the period
    cur = 0;
    if (ext_mode) {                        // external mode: wait for the next trigger
      TIMSK1 &= ~(1 << OCIE1A);
      return;
    }
    nxt = 0;
    delta = ev_period[a] - ev_t[a][ev_n[a] - 1];
  }
  cur = nxt;
  if (delta < MIN_STEP) delta = MIN_STEP;
  uint16_t s = chunk(delta);
  wait_left = delta - s;
  stepCompare(s);
}

/* Timer1 overflow: counts timer turns to measure long external periods */
ISR(TIMER1_OVF_vect) { ovf++; }

/* ------------------------------------------------------------
   Timer1 input capture: rising edge on pin 8 (external mode only).
   Measures the trigger period and restarts the waveform at t = 0.
   ------------------------------------------------------------ */
ISR(TIMER1_CAPT_vect) {
  uint16_t icr = ICR1;
  uint16_t o = ovf;
  if ((TIFR1 & (1 << TOV1)) && icr < 0x8000) o++;  // overflow happened just before the edge
  uint32_t now = ((uint32_t)o << 16) | icr;
  if (have_trig) { measured = now - last_trig; new_meas = true; }
  have_trig = true;
  last_trig = now;

  cur = 0;
  wait_left = 0;
  uint16_t target = icr + TRIG_DELAY;              // fixed delay after the edge
  if ((uint16_t)(TCNT1 - icr) > TRIG_DELAY - 8) target = TCNT1 + 8;
  OCR1A = target;
  TIFR1 = (1 << OCF1A);                            // clear any stale compare flag
  TIMSK1 |= (1 << OCIE1A);
}

/* ------------------------------------------------------------
   Build the spare event table from the channel configuration.
   Returns the number of changes that had to be shifted to keep
   MIN_STEP spacing.
   ------------------------------------------------------------ */
uint8_t buildTable() {
  noInterrupts();
  pending = false;                         // the interrupt won't touch the spare table now
  uint8_t b = active ^ 1;
  interrupts();

  uint8_t state = 0, idx[N_CH];
  for (uint8_t c = 0; c < N_CH; c++) {
    idx[c] = 0;
    if (ch_start[c]) state |= (1 << (c + 2));
  }

  uint8_t n = 1;                           // event 0 = t = 0
  ev_t[b][0] = 0;
  bool init_done = false;

  // Merge the sorted edge lists of all channels in time order
  while (true) {
    uint32_t tmin = 0xFFFFFFFFUL;
    for (uint8_t c = 0; c < N_CH; c++)
      if (idx[c] < ch_n[c] && ch_t[c][idx[c]] < tmin) tmin = ch_t[c][idx[c]];
    if (tmin == 0xFFFFFFFFUL) break;                 // no edges left
    if (!ext_mode && tmin >= cfg_period) break;      // edges after the period are ignored

    if (tmin > 0 && !init_done) { ev_p[b][0] = state; init_done = true; }

    for (uint8_t c = 0; c < N_CH; c++) {             // apply all edges at this time
      uint8_t m = (1 << (c + 2));
      while (idx[c] < ch_n[c] && ch_t[c][idx[c]] == tmin) {
        if (ch_l[c][idx[c]]) state |= m; else state &= ~m;
        idx[c]++;
      }
    }
    // edges at t = 0 only define the initial state; later ones add an event
    if (tmin > 0 && state != ev_p[b][n - 1] && n < MAX_EV) {
      ev_t[b][n] = tmin;
      ev_p[b][n] = state;
      n++;
    }
  }
  if (!init_done) ev_p[b][0] = state;

  // Enforce minimum spacing between consecutive changes
  uint8_t shifted = 0;
  for (uint8_t i = 1; i < n; i++)
    if (ev_t[b][i] < ev_t[b][i - 1] + MIN_STEP) { ev_t[b][i] = ev_t[b][i - 1] + MIN_STEP; shifted++; }

  // If shifting pushed the last change past the end of the period, stretch the period
  uint32_t per = cfg_period;
  if (!ext_mode && ev_t[b][n - 1] + MIN_STEP > per) { per = ev_t[b][n - 1] + MIN_STEP; shifted++; }

  uint8_t any = 0;
  for (uint8_t i = 0; i < n; i++) any |= ev_p[b][i];

  ev_n[b]      = n;
  ev_period[b] = per;
  ev_led[b]    = (any != 0);

  noInterrupts();
  pending = true;                          // swap at the next t = 0
  interrupts();
  return shifted;
}

// Restart the waveform in internal mode
void startInternal() {
  noInterrupts();
  ext_mode = false;
  TIMSK1 &= ~(1 << ICIE1);
  cur = 0;
  wait_left = 0;
  OCR1A = TCNT1 + 100;
  TIFR1 = (1 << OCF1A);
  TIMSK1 |= (1 << OCIE1A);
  interrupts();
}

// Switch to external mode: outputs wait for the first trigger edge
void startExternal() {
  noInterrupts();
  ext_mode  = true;
  have_trig = false;
  TIMSK1 &= ~(1 << OCIE1A);
  cur = 0;
  wait_left = 0;
  TIFR1 = (1 << ICF1);
  TIMSK1 |= (1 << ICIE1);
  interrupts();
}

/* ------------------------------------------------------------
   Command parsing
   ------------------------------------------------------------ */
// "C<n>,<s>,<t>:<l>,..." -> channel configuration. Returns error text or NULL.
const char* parseChannel(char* p) {
  char* e;
  long n = strtol(p, &e, 10);
  if (e == p || n < 1 || n > N_CH) return "bad channel";
  if (*e != ',') return "missing start level";
  p = e + 1;
  long s = strtol(p, &e, 10);
  if (e == p || (s != 0 && s != 1)) return "bad start level";
  p = e;

  uint32_t t[MAX_EDGES];
  uint8_t  l[MAX_EDGES];
  uint8_t  k = 0;
  while (*p == ',') {
    p++;
    unsigned long us = strtoul(p, &e, 10);
    if (e == p || *e != ':') return "bad edge";
    p = e + 1;
    long lv = strtol(p, &e, 10);
    if (e == p || (lv != 0 && lv != 1)) return "bad edge level";
    p = e;
    if (k >= MAX_EDGES) return "too many edges";
    if (us > 0x7FFFFFFFUL / TICKS_PER_US) return "edge too late";
    // insertion sort by time
    uint32_t tt = us * TICKS_PER_US;
    uint8_t j = k++;
    while (j > 0 && t[j - 1] > tt) { t[j] = t[j - 1]; l[j] = l[j - 1]; j--; }
    t[j] = tt;
    l[j] = (uint8_t)lv;
  }
  if (*p != '\0') return "unexpected text";

  uint8_t c = n - 1;                       // only store once everything parsed correctly
  ch_start[c] = (uint8_t)s;
  ch_n[c] = k;
  for (uint8_t i = 0; i < k; i++) { ch_t[c][i] = t[i]; ch_l[c][i] = l[i]; }
  return NULL;
}

void reply(uint8_t shifted) {
  if (shifted) { Serial.print(F("OK W")); Serial.println(shifted); }
  else Serial.println(F("OK"));
}

void handle(char* s) {
  char t = s[0];
  if (t == 'P') {
    unsigned long us = strtoul(s + 1, NULL, 10);
    if (us < MIN_PERIOD_US || us > 0x7FFFFFFFUL / TICKS_PER_US) { Serial.println(F("ERR bad period")); return; }
    cfg_period = us * TICKS_PER_US;        // takes effect at the next U
    Serial.println(F("OK"));
  } else if (t == 'C') {
    const char* err = parseChannel(s + 1);
    if (err) { Serial.print(F("ERR ")); Serial.println(err); }
    else Serial.println(F("OK"));
  } else if (t == 'X') {
    long n = strtol(s + 1, NULL, 10);
    if (n < 1 || n > N_CH) { Serial.println(F("ERR bad channel")); return; }
    ch_start[n - 1] = 0;
    ch_n[n - 1] = 0;
    Serial.println(F("OK"));
  } else if (t == 'U') {
    reply(buildTable());
  } else if (t == 'E') {
    bool ext = (s[1] == '1');
    if (ext) {
      startExternal();
      reply(buildTable());                 // in external mode edges after the period are kept
    } else {
      ext_mode = false;
      uint8_t sh = buildTable();
      startInternal();
      reply(sh);
    }
  } else {
    Serial.println(F("ERR unknown command"));
  }
}

void setup() {
  for (uint8_t i = 0; i < N_CH; i++) pinMode(i + 2, OUTPUT);   // D2..D7 = outputs
  pinMode(8, INPUT);                                           // external trigger
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  Serial.begin(9600);

  // Timer1: normal (free-running) mode, 16 MHz / 8 = 2 MHz,
  // input capture on rising edge with noise canceler
  noInterrupts();
  TCCR1A = 0;
  TCCR1B = (1 << ICNC1) | (1 << ICES1) | (1 << CS11);
  TIMSK1 = (1 << TOIE1);
  interrupts();
  startInternal();                                             // all outputs LOW until configured
}

/* ------------------------------------------------------------
   Main loop: commands, LED and reporting. Nothing here is
   time-critical; the outputs are driven by the interrupts.
   ------------------------------------------------------------ */
char line[160];
uint8_t len = 0;

void loop() {
  if (swapped) {                           // a new waveform just started
    swapped = false;
    digitalWrite(LED_PIN, ev_led[active]);
  }

  static unsigned long last_report = 0;
  if (new_meas && millis() - last_report >= 200) {   // report external period "M<us>", max 5x/s
    last_report = millis();
    noInterrupts();
    uint32_t m = measured;
    new_meas = false;
    interrupts();
    Serial.print('M');
    Serial.println(m / TICKS_PER_US);
  }

  while (Serial.available()) {             // read one line at a time
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (len) { line[len] = '\0'; handle(line); len = 0; }
    } else if (len < sizeof(line) - 1) {
      line[len++] = c;
    }
  }
}
