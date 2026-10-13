import tkinter as tk
from tkinter import ttk, font as tkfont
import serial, threading, time, json, os, re
try:
    import sv_ttk                     # modern Windows 11 style theme: pip install sv-ttk
except ImportError:
    sv_ttk = None

N_CH      = 6
MAX_EDGES = 10          # must match MAX_EDGES in the Arduino code
STEP_MS   = 0.1         # arrow step of the edge spinboxes (finer values can be typed)
COLORS    = ['#e74c3c','#3498db','#2ecc71','#f39c12','#9b59b6','#1abc9c']
THEME     = 'light'     # 'light' or 'dark'
EDGES_PER_LINE = 3      # edge boxes per line before wrapping to the next line
DEFAULT_PORT  = 'socket://beaglebone.local:9999'
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ttl_settings.json')

# Colours of the status texts and of the timeline, for each theme
PALETTES = {
    'dark':  dict(err='#ff6b6b', ok='#4cd07d', warn='#f0a35a', note='#9aa0a6', hint='#9aa0a6',
                  cv_bg='#1c1c1c', cv_lane='#262626', cv_line='#383838', cv_text='#8a8a8a',
                  cv_axis='#555555', off='#5f6368', on_btn='#2e9e5b', on_btn_act='#36b56a',
                  off_btn='#4a4f57', off_btn_act='#5a606a'),
    'light': dict(err='#d93025', ok='#1e8e3e', warn='#e37400', note='#5f6368', hint='#6b7280',
                  cv_bg='#fafafa', cv_lane='#eeeeee', cv_line='#d0d0d0', cv_text='#666666',
                  cv_axis='#999999', off='#9aa0a6', on_btn='#1e8e3e', on_btn_act='#239c46',
                  off_btn='#80868b', off_btn_act='#8f959a'),
}


def waveform(start, edges, period_us):
    """List of (t_us, level) points of the channel over one period, starting at t=0."""
    level = start
    pts = [(0, level)]
    for t, l in edges:
        if t < period_us and l != level:
            if t == 0:
                pts[0] = (0, l)
            else:
                pts.append((t, l))
            level = l
    return pts


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("TTL Waveform Generator")
        self.root.resizable(False, False)
        self._setup_style()
        self.ser = None
        self._after_id = None
        self._reading  = False
        self._loading  = False
        self._unsent   = False   # True if settings changed while not connected
        self._build()
        self._load_settings()
        for i in range(N_CH):
            self._rebuild_edges(i)
        self._validate()
        self._draw_timeline()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ── Style ──────────────────────────────────────────────
    def _setup_style(self):
        theme = THEME if sv_ttk else 'light'
        if sv_ttk:
            sv_ttk.set_theme(theme)
            for name, size in (('SunValleyBodyFont', -13), ('SunValleyBodyStrongFont', -13),
                               ('SunValleyCaptionFont', -12)):
                try: tkfont.nametofont(name).configure(size=size)
                except tk.TclError: pass
        else:
            print("Tip: 'pip install sv-ttk' for the modern look")
        self.pal = PALETTES[theme]
        self.font = 'Segoe UI'
        for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont'):
            try: tkfont.nametofont(name).configure(family=self.font, size=9)
            except tk.TclError: pass
        st = ttk.Style()
        st.configure('Header.TLabel', font=(self.font, 9, 'bold'), foreground=self.pal['hint'])
        st.configure('Hint.TLabel', font=(self.font, 9), foreground=self.pal['hint'])
        st.configure('Bad.TSpinbox', foreground=self.pal['err'])

    def _mark(self, sb, bad):
        """Show / clear the 'invalid value' look of a spinbox."""
        sb.configure(style='Bad.TSpinbox' if bad else 'TSpinbox')
        try: sb.state(['invalid'] if bad else ['!invalid'])
        except tk.TclError: pass

    # ── Interface ──────────────────────────────────────────
    def _build(self):
        pad = dict(padx=8, pady=4)

        # Connexion and Cycle side by side
        top = ttk.Frame(self.root)
        top.grid(row=0, column=0, columnspan=2, sticky='ew')
        f = ttk.LabelFrame(top, text="Arduino", padding=8)
        f.pack(side='left', fill='both', **pad)
        ttk.Label(f, text="Port:").grid(row=0, column=0)
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ttk.Entry(f, textvariable=self.port_var, width=30).grid(row=0, column=1, padx=4)
        self.btn_conn = ttk.Button(f, text="Connect", width=10, command=self._connect)
        self.btn_conn.grid(row=0, column=2, padx=6)
        self.lbl_status = ttk.Label(f, text="◉ disconnected", foreground=self.pal['err'])
        self.lbl_status.grid(row=0, column=3, padx=6)

        # Cycle
        f2 = ttk.LabelFrame(top, text="Cycle", padding=8)
        f2.pack(side='left', fill='both', expand=True, **pad)
        ttk.Label(f2, text="Period (ms):").grid(row=0, column=0)
        self.period = tk.StringVar(value="100.000")
        self.spin_period = ttk.Spinbox(f2, from_=0.05, to=1e6, increment=1.0, format="%.3f",
                                       textvariable=self.period, width=11)
        self.spin_period.grid(row=0, column=1, padx=6)
        self.ext_var = tk.BooleanVar(value=False)
        ttk.Radiobutton(f2, text="Internal", variable=self.ext_var,
                        value=False, command=self._on_ext_toggle).grid(row=0, column=2, padx=6)
        ttk.Radiobutton(f2, text="External (pin 8)", variable=self.ext_var,
                        value=True,  command=self._on_ext_toggle).grid(row=0, column=3, padx=6)
        self.period.trace_add('write', self._on_change)

        # Channels
        f3 = ttk.LabelFrame(self.root, text="Channels   (edge times in ms — each edge switches the level)",
                            padding=8)
        f3.grid(row=1, column=0, sticky='nsew', **pad)
        for c, txt in enumerate(['', 'Name', 'En', 'Start', '', 'Edges']):
            ttk.Label(f3, text=txt, style='Header.TLabel').grid(row=0, column=c, padx=4, sticky='w')

        self.en, self.start_lvl, self.names = [], [], []
        self.edge_vars   = [[] for _ in range(N_CH)]   # one StringVar per edge (ms)
        self.edge_spins  = [[] for _ in range(N_CH)]
        self.edge_frames, self.btn_add, self.btn_del = [], [], []
        for i in range(N_CH):
            r = i + 1
            tk.Label(f3, text=f'{i+1}', bg=COLORS[i], fg='white', width=3,
                     font=(self.font, 9, 'bold')).grid(row=r, column=0, pady=3, padx=4, sticky='n')
            nm = tk.StringVar(value='')
            ttk.Entry(f3, textvariable=nm, width=14).grid(row=r, column=1, padx=(0, 4), pady=2, sticky='n')
            nm.trace_add('write', self._on_name_change)
            self.names.append(nm)
            en = tk.BooleanVar(value=False)
            ttk.Checkbutton(f3, variable=en, command=self._on_change).grid(row=r, column=2, sticky='n')
            st = tk.StringVar(value='L')
            ttk.Combobox(f3, textvariable=st, values=('L', 'H'), width=3,
                         state='readonly').grid(row=r, column=3, padx=4, pady=2, sticky='n')
            st.trace_add('write', lambda *_, i=i: self._on_start_change(i))
            bf = ttk.Frame(f3)
            bf.grid(row=r, column=4, padx=(4, 0), pady=2, sticky='nw')
            ba = ttk.Button(bf, text='+', width=2, command=lambda i=i: self._add_edge(i))
            bd = ttk.Button(bf, text='−', width=2, command=lambda i=i: self._del_edge(i))
            ba.pack(side='left'); bd.pack(side='left', padx=(2, 0))
            ef = ttk.Frame(f3)
            ef.grid(row=r, column=5, sticky='nw')
            self.en.append(en); self.start_lvl.append(st)
            self.btn_add.append(ba); self.btn_del.append(bd); self.edge_frames.append(ef)
        self.lbl_msg = ttk.Label(f3, text="", foreground=self.pal['err'])
        self.lbl_msg.grid(row=N_CH+1, column=0, columnspan=6, sticky='w', padx=4, pady=(4, 0))

        # Timeline
        self.CW, self.CH = 420, 300
        self.canvas = tk.Canvas(self.root, width=self.CW, height=self.CH, bg=self.pal['cv_bg'],
                                highlightthickness=1, highlightbackground=self.pal['cv_line'])
        self.canvas.grid(row=1, column=1, padx=8, pady=(12, 4), sticky='n')

        # Master ON/OFF
        f4 = ttk.LabelFrame(self.root, text="All outputs", padding=8)
        f4.grid(row=2, column=0, columnspan=2, sticky='ew', **pad)
        self.master_on = tk.BooleanVar(value=True)
        self.btn_master = tk.Button(f4, font=(self.font, 10, 'bold'), fg='white',
                                    activeforeground='white', relief='flat', bd=0,
                                    pady=4, cursor='hand2', command=self._toggle_master)
        self.btn_master.pack(fill='x')
        self._update_master_button()

        ttk.Button(self.root, text="⟳  Send to Arduino", style='Accent.TButton',
                   command=self._send_all).grid(row=3, column=0, columnspan=2,
                                                sticky='ew', padx=8, pady=(4, 10))

    # ── Edges of a channel ─────────────────────────────────
    def _start(self, i):
        return 1 if self.start_lvl[i].get() == 'H' else 0

    def _rebuild_edges(self, i):
        """Recreate the spinboxes of channel i (after adding/removing an edge)."""
        fr = self.edge_frames[i]
        for w in fr.winfo_children():
            w.destroy()
        self.edge_spins[i] = []
        start = self._start(i)
        for k, var in enumerate(self.edge_vars[i]):
            lvl = start ^ ((k + 1) % 2)                    # levels alternate: t1 = opposite of start
            ttk.Label(fr, text=f"t{k+1}{'↑' if lvl else '↓'}",
                      style='Hint.TLabel').grid(row=k // EDGES_PER_LINE,
                                                column=2 * (k % EDGES_PER_LINE), padx=(8, 2))
            sb = ttk.Spinbox(fr, textvariable=var, from_=0, to=1e7, increment=STEP_MS,
                             format='%.3f', width=7,
                             command=lambda i=i, k=k: self._clamp(i, k))
            sb.grid(row=k // EDGES_PER_LINE, column=2 * (k % EDGES_PER_LINE) + 1, pady=2)
            sb.bind('<FocusOut>', lambda e, i=i, k=k: self._clamp(i, k))
            sb.bind('<Return>',   lambda e, i=i, k=k: self._clamp(i, k))
            self.edge_spins[i].append(sb)
        n = len(self.edge_vars[i])
        self.btn_add[i].config(state='disabled' if n >= MAX_EDGES else 'normal')
        self.btn_del[i].config(state='disabled' if n == 0 else 'normal')

    def _new_edge_var(self, value_ms):
        var = tk.StringVar(value=f"{value_ms:.3f}")
        var.trace_add('write', self._on_change)
        return var

    def _add_edge(self, i):
        vals = self.edge_vars[i]
        if len(vals) >= MAX_EDGES: return
        period = self._period_ms() or 100.0
        try: last = float(vals[-1].get()) if vals else 0.0
        except ValueError: last = 0.0
        hi = period - 0.001
        if last >= hi:
            self.lbl_msg.config(text=f"Ch{i+1}: no room left before the end of the period",
                                foreground=self.pal['warn'])
            return
        step = max(STEP_MS, round(period * 0.1, 1))         # default: 10 % of the period later
        new = min(last + step, hi) if vals else min(step, hi)
        vals.append(self._new_edge_var(new))
        self._rebuild_edges(i)
        self._on_change()

    def _del_edge(self, i):
        if not self.edge_vars[i]: return
        self.edge_vars[i].pop()
        self._rebuild_edges(i)
        self._on_change()

    def _on_start_change(self, i):
        self._rebuild_edges(i)                              # update ↑/↓ labels
        self._on_change()

    def _clamp(self, i, k):
        """Keep edge k between the previous edge and the next one (or the end of the period)."""
        vals = self.edge_vars[i]
        if k >= len(vals): return
        try: v = float(vals[k].get())
        except ValueError: return                           # not a number: stays red
        lo, hi = 0.0, None
        if k > 0:
            try: lo = float(vals[k-1].get())
            except ValueError: pass
        if k + 1 < len(vals):
            try: hi = float(vals[k+1].get())
            except ValueError: pass
        elif self._period_ms():
            hi = self._period_ms() - 0.001
        v2 = max(v, lo)
        if hi is not None: v2 = min(v2, hi)
        if abs(v2 - v) > 1e-9:
            vals[k].set(f"{v2:.3f}")

    # ── Master ON/OFF ─────────────────────────────────────
    def _update_master_button(self):
        if self.master_on.get():
            self.btn_master.config(text="●  OUTPUTS ON  —  click to turn all OFF",
                                   bg=self.pal['on_btn'], activebackground=self.pal['on_btn_act'])
        else:
            self.btn_master.config(text="○  OUTPUTS OFF  —  click to turn all ON",
                                   bg=self.pal['off_btn'], activebackground=self.pal['off_btn_act'])

    def _toggle_master(self):
        # Channel settings are kept; only what is sent to the Arduino changes.
        self.master_on.set(not self.master_on.get())
        self._update_master_button()
        self._send_all()

    # ── Clock externe ──────────────────────────────────────
    def _on_ext_toggle(self):
        ext = self.ext_var.get()
        self.spin_period.config(state='disabled' if ext else 'normal')
        self._send('E1' if ext else 'E0')
        self._on_change()

    # ── Validation ─────────────────────────────────────────
    def _period_ms(self):
        try:
            p = float(self.period.get())
            return p if p >= 0.05 else None
        except ValueError:
            return None

    def _period_us(self):
        p = self._period_ms()
        return int(round(p * 1000)) if p else None

    def _channel(self, i, period_us):
        """Returns (start, [(t_us, level)], error, bad_spinbox_indexes) for channel i."""
        start = self._start(i)
        edges, bad, err = [], [], None
        prev = 0
        for k, var in enumerate(self.edge_vars[i]):
            lvl = start ^ ((k + 1) % 2)
            try:
                t = int(round(float(var.get()) * 1000))
            except ValueError:
                bad.append(k); err = err or f"t{k+1} is not a number"; continue
            if t < prev:
                bad.append(k); err = err or f"t{k+1} is before t{k}"
            elif period_us and not self.ext_var.get() and t >= period_us:
                bad.append(k); err = err or f"t{k+1} is after the end of the period"
            edges.append((t, lvl))
            prev = max(prev, t)
        return start, edges, err, bad

    def _validate(self):
        """Check all channels. Returns list of (start, edges) or None if something is wrong."""
        period_us = self._period_us()
        ext = self.ext_var.get()
        result, errors = [], []
        if period_us is None and not ext:
            errors.append("period must be a number ≥ 0.05 ms")
        for i in range(N_CH):
            start, edges, err, bad = self._channel(i, period_us)
            for k, sb in enumerate(self.edge_spins[i]):
                self._mark(sb, k in bad)
            if err and self.en[i].get():
                errors.append(f"Ch{i+1}: {err}")
            result.append((start, edges))
        if errors:
            self.lbl_msg.config(text="Not sent — " + errors[0], foreground=self.pal['err'])
            return None
        if str(self.lbl_msg.cget('foreground')) == self.pal['err']:
            self.lbl_msg.config(text="")
        return result

    def _on_name_change(self, *_):
        # Names only live in the GUI: redraw the preview and save, nothing is sent
        if self._loading: return
        self._draw_timeline()
        self._save_settings()

    def _on_change(self, *_):
        if self._loading: return
        if self._after_id: self.root.after_cancel(self._after_id)
        self._after_id = self.root.after(300, self._send_all)

    # ── Sauvegarde ─────────────────────────────────────────
    def _get_settings(self):
        def num(v):
            try: return round(float(v.get()), 3)
            except ValueError: return v.get()
        return {
            'version':   3,
            'port':      self.port_var.get(),
            'period_ms': self.period.get(),
            'external':  bool(self.ext_var.get()),
            'master_on': bool(self.master_on.get()),
            'channels':  [{'en':    bool(self.en[i].get()),
                           'name':  self.names[i].get(),
                           'start': self.start_lvl[i].get(),
                           'edges': [num(v) for v in self.edge_vars[i]]} for i in range(N_CH)],
        }

    def _save_settings(self):
        if self._loading: return
        try:
            tmp = SETTINGS_FILE + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(self._get_settings(), fh, indent=2)
            os.replace(tmp, SETTINGS_FILE)
        except Exception as ex:
            print('Could not save settings:', ex)

    @staticmethod
    def _convert_old_channel(ch):
        """Older settings files -> (start 'L'/'H', [edge times in ms])."""
        edges = ch.get('edges')
        if isinstance(edges, list):                         # current format
            return ch.get('start', 'L'), edges
        if isinstance(edges, str):                          # text format "1.5 H, 3 L"
            pts = []
            for tok in re.split(r'[,;]', edges):
                m = re.match(r'^\s*(\d+(?:\.\d*)?|\.\d+)\s*(?:ms)?\s*(?:->|→|:|=)?\s*([HhLl01])\s*$', tok)
                if m:
                    pts.append((float(m.group(1)), 1 if m.group(2) in 'Hh1' else 0))
            level = 1 if ch.get('start', 'L') == 'H' else 0
            times = []
            for t, l in sorted(pts):
                if t == 0: level = l
                elif l != level: times.append(t); level = l
            start_level = 1 if ch.get('start', 'L') == 'H' else 0
            for t, l in sorted(pts):
                if t == 0: start_level = l
            return ('H' if start_level else 'L'), times
        s, e = ch.get('start', 0.0), ch.get('end', 0.0)     # single-pulse format
        try: s, e = float(s), float(e)
        except (TypeError, ValueError): return 'L', []
        return 'L', ([s, e] if e > s else [])

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE) as fh:
                d = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as ex:
            print('Could not load settings:', ex)
            return
        self._loading = True
        try:
            self.port_var.set(d.get('port', DEFAULT_PORT))
            period = d.get('period_ms', d.get('period', 100.0))
            try: self.period.set(f"{float(period):.3f}")
            except ValueError: self.period.set(str(period))
            self.ext_var.set(d.get('external', False))
            self.master_on.set(d.get('master_on', True))
            for i, ch in enumerate(d.get('channels', [])[:N_CH]):
                self.en[i].set(ch.get('en', False))
                start, times = self._convert_old_channel(ch)
                self.start_lvl[i].set(start)
                self.names[i].set(ch.get('name', ''))
                self.edge_vars[i] = []
                for t in times[:MAX_EDGES]:
                    try: self.edge_vars[i].append(self._new_edge_var(float(t)))
                    except (TypeError, ValueError): pass
        finally:
            self._loading = False
        self.spin_period.config(state='disabled' if self.ext_var.get() else 'normal')
        self._update_master_button()

    # ── Connexion série ────────────────────────────────────
    def _is_connected(self):
        return self.ser is not None and self.ser.is_open

    def _connect(self):
        if self._is_connected():
            self._disconnect('◉ disconnected')
            return
        try:
            port = self.port_var.get().strip()
            if '://' in port:
                ser = serial.serial_for_url(port, baudrate=9600, timeout=0.1)
            else:
                ser = serial.Serial(port, 9600, timeout=0.1)
        except Exception as ex:
            self.lbl_status.config(text=f'Error: {ex}', foreground=self.pal['err'])
            return
        self.ser = ser
        self.btn_conn.config(text='Disconnect')
        self.lbl_status.config(text='connecting…', foreground=self.pal['warn'])
        self._reading = True
        threading.Thread(target=self._serial_reader, args=(ser,), daemon=True).start()
        self.root.after(2000, lambda: self._on_connected(ser))
        self._save_settings()

    def _on_connected(self, ser):
        if self.ser is not ser: return
        self.lbl_status.config(text='◉ connected', foreground=self.pal['ok'])
        # Connecting does NOT resend anything: the Arduino keeps running its waveform.
        if self._unsent:
            self._send_all()

    def _disconnect(self, msg, color=None):
        self._reading = False
        ser, self.ser = self.ser, None
        if ser:
            try: ser.close()
            except: pass
        self.btn_conn.config(text='Connect')
        self.lbl_status.config(text=msg, foreground=color or self.pal['err'])

    def _serial_reader(self, ser):
        """Thread: reads replies from the Arduino."""
        while self._reading and self.ser is ser:
            try:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if line.startswith('M'):                       # measured period, in µs
                    us = int(line[1:])
                    self.root.after(0, lambda v=us: self._set_measured(v))
                elif line.startswith('ERR'):
                    self.root.after(0, lambda t=line: self.lbl_msg.config(
                        text=f"Arduino: {t}", foreground=self.pal['err']))
                elif line.startswith('OK W'):
                    self.root.after(0, lambda t=line: self.lbl_msg.config(
                        text=f"note: {t[4:]} edge(s) closer than 20 µs were shifted slightly",
                        foreground=self.pal['note']))
            except serial.SerialException:
                self.root.after(0, lambda: self.ser is ser and
                                self._disconnect('◉ connection lost'))
                return
            except Exception:
                time.sleep(0.1)

    def _set_measured(self, us):
        # Show the measured external period without triggering a resend
        self._loading = True
        try: self.period.set(f"{us/1000:.3f}")
        finally: self._loading = False
        self._draw_timeline()

    # ── Envoi ──────────────────────────────────────────────
    def _send(self, cmd):
        if self._is_connected():
            try: self.ser.write((cmd + '\n').encode())
            except: pass

    def _send_all(self):
        self._after_id = None
        chans = self._validate()
        self._draw_timeline(chans)
        self._save_settings()
        if chans is None:
            return                                   # error: keep the Arduino as it is
        self._unsent = not self._is_connected()

        if not self.ext_var.get():
            self._send(f"P{self._period_us()}")
        master = self.master_on.get()
        for i, (start, edges) in enumerate(chans):
            if master and self.en[i].get():
                parts = [f"C{i+1}", str(start)] + [f"{t}:{l}" for t, l in edges]
                self._send(",".join(parts))
            else:
                self._send(f"X{i+1}")
        self._send("U")                              # apply all channels together

    def _on_close(self):
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._send_all()
        self._save_settings()
        self._disconnect('◉ disconnected')
        self.root.destroy()

    # ── Timeline ──────────────────────────────────────────
    def _draw_timeline(self, chans=None):
        c = self.canvas; c.delete('all')
        W, H = self.CW, self.CH
        has_names = any(n.get().strip() for n in self.names)
        ML, MR, MT = (96 if has_names else 24), 10, 18
        period_us = self._period_us()
        if not period_us:
            return
        if chans is None:
            chans = [self._channel(i, period_us)[:2] for i in range(N_CH)]
        master = self.master_on.get()
        title = f"T = {period_us/1000:g} ms" + ("" if master else "   (ALL OFF)")
        c.create_text(W//2, 9, text=title, fill=self.pal['cv_text'], font=(self.font, 8))
        bg = 8
        bw, bh = W - ML - MR, int((H - MT - 30) / N_CH) - bg
        x_of = lambda t: ML + bw * min(t, period_us) / period_us
        for i in range(N_CH):
            y = MT + i * (bh + bg)
            c.create_rectangle(ML, y, ML+bw, y+bh, fill=self.pal['cv_lane'], outline=self.pal['cv_line'])
            name = self.names[i].get().strip()
            if len(name) > 11: name = name[:10] + '…'
            label = f"{i+1}  {name}" if name else str(i+1)
            c.create_text(ML-4, y+bh//2, text=label, fill=COLORS[i],
                          font=(self.font, 8, 'bold'), anchor='e')
            if not self.en[i].get():
                continue
            start, edges = chans[i]
            pts = waveform(start, edges, period_us)
            yh, yl = y + 4, y + bh - 4
            coords = []
            for k, (t, l) in enumerate(pts):
                x = x_of(t)
                yy = yh if l else yl
                if k > 0:
                    coords += [x, coords[-1]]            # vertical edge
                coords += [x, yy]
            coords += [ML + bw, coords[-1]]
            color = COLORS[i] if master else self.pal['off']
            c.create_line(*coords, fill=color, width=2, dash=() if master else (3, 2))
        ay = MT + N_CH * (bh + bg) + 2
        c.create_line(ML, ay, ML+bw, ay, fill=self.pal['cv_axis'])
        for pct in [0, 25, 50, 75, 100]:
            x = ML + bw * pct / 100
            c.create_line(x, ay-3, x, ay+3, fill=self.pal['cv_axis'])
            c.create_text(x, ay+9, text=f"{period_us/1000*pct/100:g}", fill=self.pal['cv_text'],
                          font=(self.font, 7))


root = tk.Tk()
App(root)
root.mainloop()
