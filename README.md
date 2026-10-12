# TTL Delay Generator

6-channel TTL waveform / delay generator on an Arduino Uno, controlled from a Python GUI over the network through a BeagleBone (or Raspberry Pi) bridge. Built for sample-and-hold timing in the lab.

```
 Lab PC (Python GUI)  ──Ethernet──►  BeagleBone (bridge)  ──USB──►  Arduino Uno  ──►  6 TTL outputs
```

## Contents

- [Control chain](#control-chain)
- [Setting up the delay generator](#setting-up-the-delay-generator)
- [Arduino code](#arduino-code)
- [Setting up the Arduino](#setting-up-the-arduino)
- [Bridge between PC and Arduino: BeagleBone (or Raspberry Pi)](#bridge-between-pc-and-arduino-beaglebone-or-raspberry-pi)
- [Graphical User Interface](#graphical-user-interface)

---

## Control chain

Before going into how the delay generator works, here is how we communicate with the Arduino.

The control chain has three parts:

- **Lab PC.** Runs the Python GUI, where we set the period and the switching times of each channel. It does not talk to the Arduino directly, but sends short text commands over Ethernet (through the lab router) to the BeagleBone.
- **BeagleBone Black.** A small Linux computer similar to a Raspberry Pi. It runs a small bridge program that starts automatically when it boots: it listens for the GUI's connection on the network (`beaglebone.local`, port `9999`) and passes every command unchanged to the Arduino over USB.
- **Arduino.** The part that actually generates the TTL pulses, using its own hardware timer, so the timing precision depends only on the Arduino and not on the PC or the network.

### Why a BeagleBone in the middle?

We could plug the Arduino straight into the PC by USB, but we put the BeagleBone in between to keep the pulse generation independent of the PC. The lab PC is used for many other things, and if it crashes, reboots or turns off, a direct USB connection could reset the Arduino or cut its power and stop the pulses in the middle of a measurement.

With this setup, the Arduino gets its power and connection from the BeagleBone, which does nothing else and stays on. Once the settings are sent, the Arduino keeps outputting them even if the GUI is closed or the PC goes down. The GUI also saves its settings to a file and does not resend anything when it reconnects, so restarting the PC or the GUI does not interrupt the outputs.

---

## Setting up the delay generator

1. **Upload the code to the Arduino.** Connect the Arduino to a PC by USB and upload `ttl_edges.ino` with Arduino IDE (see [Setting up the Arduino](#setting-up-the-arduino)). This only needs to be done once, or after changing the code.
2. **Set up the bridge on the BeagleBone (or Raspberry Pi).** Plug the Arduino into the BeagleBone by USB, connect the BeagleBone to the lab router by Ethernet, and install the bridge service (see [Bridge between PC and Arduino](#bridge-between-pc-and-arduino-beaglebone-or-raspberry-pi)). This is also a one-time setup; afterwards the bridge starts by itself whenever the BeagleBone is powered.
3. **Launch the GUI on the lab PC.** Run the Python script, click **Connect**, and set the period and channels. Changes are sent automatically, and the settings are saved for the next time.

---

## Arduino code

The Arduino has **6 outputs (pins D2 to D7)**, one per channel.

For each channel, we choose whether it **starts LOW or HIGH**, then the **times at which it switches**. For example, *"start LOW, switch at 1 ms and at 3 ms"* gives a pulse from 1 ms to 3 ms. A channel can switch up to **10 times per period**, so it can produce several pulses of any shape. This pattern repeats every period.

In **external mode**, it restarts on each trigger signal received on **pin 8** instead.

The Arduino uses its internal clock to switch each output at the right moment, independently of the PC and the network, so the timing stays precise (to within a few microseconds). Two different switching times should be at least **20 µs** apart, which is not a limitation in normal use. When new settings are sent from the GUI, they take effect at the start of the next period, so the outputs never glitch. The **LED** on the box is on whenever at least one output is active.

> **Note:** the Arduino keeps its settings only while it is powered. After a power cut or a reflash, click **Send to Arduino** in the GUI to restore them.

---

## Setting up the Arduino

1. **Install Arduino IDE.** Download it from [arduino.cc/en/software](https://www.arduino.cc/en/software), install it, and open it.
2. **Connect the Arduino directly to your PC** via USB.
3. **Open the code.** *File → Open →* `ttl_edges.ino`. If the IDE says the file must be in a folder with the same name, accept; it creates the folder automatically. You can also simply paste the code into a new sketch.
4. **Select the board.** *Tools → Board → Arduino AVR Boards →* **Arduino Uno**.
5. **Select the port.** *Tools → Port →* the port marked **(Arduino Uno)**, e.g. `COM4`.
6. **Upload.** Click the arrow button (→) at the top left. Wait for *"Done uploading"* at the bottom.
7. You're now ready to set up the link with the BeagleBone or the Raspberry Pi.

**If something goes wrong**

- **No port appears in step 5:** try another USB cable. Some cables only carry power, not data.
- **The upload fails with "port busy" or "access denied":** something else is using the Arduino. Close the Serial Monitor in the IDE, or any program connected to that COM port, and try again.

---

## Bridge between PC and Arduino: BeagleBone (or Raspberry Pi)

The BeagleBone runs a small program called **socat** that listens on the network (port `9999`) and forwards everything it receives to the Arduino over USB, and the Arduino's replies back. It is installed as a service, so it starts automatically every time the BeagleBone boots, and it restarts by itself if the Arduino is unplugged and plugged back in.

### Setup

1. **Connect** the BeagleBone to the lab router with Ethernet, and the Arduino to the BeagleBone with USB.
   *The BeagleBone needs to be authorized on the router; follow the same procedure as for the Red Pitayas.*

2. **Log in** from a PC terminal (Command Prompt). The password is `temppwd`.
   ```bash
   ssh debian@beaglebone.local
   ```

3. **Set the date.** The BeagleBone has no clock battery, so its date resets after every reboot, which breaks downloads. Use today's date:
   ```bash
   sudo date -s "2026-10-12"
   ```

4. **Old image only (Debian 10 "Buster"):** point the package sources to the Debian archive.
   ```bash
   sudo sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g; /buster-updates/d' /etc/apt/sources.list
   ```

5. **Install socat:**
   ```bash
   sudo apt-get update
   sudo apt-get install -y socat
   ```

6. **Create the service:**
   ```bash
   sudo nano /etc/systemd/system/arduino-bridge.service
   ```
   Paste the following, then save with `Ctrl+X`, `Y`, `Enter`:
   ```ini
   [Unit]
   Description=Arduino serial bridge
   After=network.target
   StartLimitIntervalSec=0

   [Service]
   ExecStartPre=/bin/sh -c 'if [ -f /dev/ttyACM0 ]; then rm -f /dev/ttyACM0; fi; test -c /dev/ttyACM0'
   ExecStart=/usr/bin/socat TCP-LISTEN:9999,reuseaddr,keepalive,keepidle=10,keepintvl=5,keepcnt=3 OPEN:/dev/ttyACM0,b9600,raw,echo=0,hupcl=0
   Restart=always
   RestartSec=3

   [Install]
   WantedBy=multi-user.target
   ```

7. **Enable and start it:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable arduino-bridge
   sudo systemctl start arduino-bridge
   ```

8. **Check it is running.** The status should say `active (running)`; press `q` to exit.
   ```bash
   sudo systemctl status arduino-bridge
   ```

In the GUI, the port is `socket://beaglebone.local:9999`.

### With a Raspberry Pi or another board

The steps are the same, but skip step 4. The login name and hostname will differ (e.g. `raspberrypi.local`); use that hostname in the GUI's port field. If the Arduino doesn't appear as `/dev/ttyACM0` (check with `ls /dev/ttyACM* /dev/ttyUSB*`), replace that name in the service file.

### Troubleshooting

If the GUI connects but the Arduino doesn't respond, check the Arduino's port:
```bash
ls -l /dev/ttyACM0
```
The line should start with `c`. If it starts with `-`, a normal file is blocking the Arduino's port. Unplug and replug the Arduino; the service removes that file by itself.

---

## Graphical User Interface

The GUI is a Python script that uses the **tkinter** module for the window and **pyserial** to talk to the Arduino through the BeagleBone. It runs on the lab PC and has four parts:

| Part | What it does |
|---|---|
| **Arduino** | The address of the bridge (`socket://beaglebone.local:9999`) and a **Connect** button. Connecting doesn't send anything, so the Arduino keeps running its current settings. |
| **Cycle** | The period in ms, and the choice between internal timing and an external trigger on pin 8. In external mode, the measured trigger period is displayed. |
| **Channels** | For each of the 6 channels: an enable box, the start level (L or H), and the switching times. The **+** and **−** buttons add or remove a switching time, up to 10 per channel. The ↑/↓ arrows show whether each switching time goes up or down. A preview on the right draws the resulting signals over one period. |
| **All outputs** | One button that turns every output off or back on, without losing the channel settings. |

- Changes are sent to the Arduino automatically about 0.3 s after you edit a value. **Send to Arduino** sends everything again by hand, e.g. after reflashing the Arduino.
- An invalid value (a switching time placed before the previous one, or after the end of the period) turns red, and nothing is sent until it's fixed.
- All settings are saved automatically in `ttl_settings.json`, next to the script, and reloaded when the GUI opens.

### Running it

Python needs the pyserial module (tkinter comes with Python on Windows):
```bash
pip install pyserial
python ttl_gui.py
```
