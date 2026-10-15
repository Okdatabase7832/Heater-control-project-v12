import machine
import network
import socket
import time
import onewire, ds18x20
import neopixel
import gc

# ------------------ CONSTANTS ------------------
FAIL_TIMEOUT = 300
WDT_TIMEOUT = 8000
BRIGHTNESS = 0.15

HISTORY_SIZE = 180
DAILY_REBOOT_MS = 86400000      # 24 hours
FLASH_INTERVAL = 500            # ms
GC_INTERVAL = 60000             # ms

USE_WDT = True                  # master switch for the watchdog
DEBUG_PIN = 22                  # GP22 tied to GND at power-up = debug boot (no watchdog)

NEO_TIMEOUT = 5000              # ms without a pixel update -> re-create driver
NEO_MAX_RESETS = 5

# ------------------ STATE ------------------
last_ext_temp = None
last_temp_change = time.time()
heater_on = False
active_led = "None"

internal_temp = 0.0
external_temp = 0.0

alarm_enabled = True
alarm_latched = False
alarm_acknowledged = False
alarm_rearm = True              # alarm may trigger again once temp has been back in range
alarm_flash_state = False
last_flash_time = 0

ds_conversion_started = False
ds_conversion_time = 0

last_gc = time.ticks_ms()
last_neo_update = time.ticks_ms()
neo_reset_count = 0

# ------------------ BOOT / RESET INFO ------------------
boot_time = time.ticks_ms()

def get_reset_cause():
    cause = machine.reset_cause()
    if cause == machine.PWRON_RESET:
        return "Power-on / Power loss"
    elif cause == machine.WDT_RESET:
        return "Watchdog reset"
    elif cause == machine.SOFT_RESET:
        return "Soft reset"
    elif cause == machine.HARD_RESET:
        return "Hard reset"
    else:
        return "Unknown"

reset_cause = get_reset_cause()
print("Reset cause:", reset_cause)

# Debug boot: tie GP22 to GND before powering up and the watchdog stays off,
# so Thonny / VS Code / mpremote can interrupt the program without the board
# resetting (and the USB port vanishing) 8 seconds later.
debug_pin = machine.Pin(DEBUG_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
use_wdt = USE_WDT and debug_pin.value() == 1

# ------------------ GPIO ------------------
led_onboard = machine.Pin("LED", machine.Pin.OUT)
led_onboard.on()

leds = {
    "Blue": machine.Pin(19, machine.Pin.OUT),
    "Green": machine.Pin(11, machine.Pin.OUT),
    "Yellow": machine.Pin(8, machine.Pin.OUT),
    "Red": machine.Pin(4, machine.Pin.OUT),
    "White": machine.Pin(15, machine.Pin.OUT),
}

heater = machine.Pin(16, machine.Pin.OUT)

# ------------------ NEOPIXEL ------------------
NUM_PIXELS = 7
NEOPIXEL_PIN = 0       # GP0
STATE_MACHINE = 0

def make_np():
    # Same constructor everywhere (the original reset code used a different one)
    return neopixel.NeoPixel(NUM_PIXELS, STATE_MACHINE, NEOPIXEL_PIN, "GRB")

np = make_np()

PIXEL_COLORS = {
    "Blue":   (0, 0, 255),
    "Green":  (0, 255, 0),
    "Yellow": (255, 150, 0),
    "Red":    (255, 0, 0),
    "White":  (255, 255, 255),
}

def scale(color):
    return tuple(int(c * BRIGHTNESS) for c in color)

ALARM_BRIGHT = scale((255, 255, 255))
ALARM_DIM = scale((10, 10, 10))

def np_set(color):
    """Fill + show. Errors are printed instead of silently swallowed."""
    global last_neo_update
    try:
        np.fill(color)
        np.show()
        last_neo_update = time.ticks_ms()
    except Exception as e:
        print("NeoPixel error:", e)

# ------------------ TEMP SENSORS ------------------
adc_temp = machine.ADC(4)
conversion_factor = 3.3 / 65535

ow = onewire.OneWire(machine.Pin(26))
ds = ds18x20.DS18X20(ow)
roms = ds.scan()
if not roms:
    raise RuntimeError("No DS18B20 detected")

# ------------------ HISTORY BUFFER ------------------
history_ext = [0] * HISTORY_SIZE
history_int = [0] * HISTORY_SIZE
history_index = 0
history_count = 0

# ------------------ WIFI AP ------------------
ap = network.WLAN(network.AP_IF)
ap.config(essid="Pico_W", password="password")   # configure first, then activate
ap.active(True)

t0 = time.ticks_ms()
while not ap.active():
    if time.ticks_diff(time.ticks_ms(), t0) > 5000:
        raise RuntimeError("AP failed to start")
    time.sleep_ms(100)
print("AP active:", ap.ifconfig())

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(socket.getaddrinfo("0.0.0.0", 80)[0][-1])
srv.listen(4)
srv.settimeout(0.05)
print("Web server started")

# ------------------ HELPERS ------------------
def get_uptime():
    seconds = time.ticks_diff(time.ticks_ms(), boot_time) // 1000
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    return "{}d {:02d}:{:02d}:{:02d}".format(d, h, m, s)

def read_internal_temp():
    reading = adc_temp.read_u16() * conversion_factor
    return 27 - (reading - 0.706) / 0.001721

def daily_reboot_check():
    if time.ticks_diff(time.ticks_ms(), boot_time) > DAILY_REBOOT_MS:
        machine.reset()

# --- DS18B20 non-blocking ---
def start_temp_conversion():
    global ds_conversion_started, ds_conversion_time
    ds.convert_temp()
    ds_conversion_started = True
    ds_conversion_time = time.ticks_ms()

def read_external_temp_nonblocking():
    global ds_conversion_started
    if not ds_conversion_started:
        start_temp_conversion()
        return None
    if time.ticks_diff(time.ticks_ms(), ds_conversion_time) >= 750:
        ds_conversion_started = False
        return ds.read_temp(roms[0])
    return None

start_temp_conversion()

def clear_leds():
    for l in leds.values():
        l.off()

def update_leds(temp):
    global active_led

    # Alarm overrides normal LED display
    if alarm_latched and not alarm_acknowledged:
        return

    clear_leds()
    active_led = "None"

    if 0 <= temp < 30:
        active_led = "Blue"
    elif 30 <= temp < 40:
        active_led = "Green"
    elif 40 <= temp < 50:
        active_led = "Yellow"
    elif 50 <= temp < 60:
        active_led = "Red"
    elif 60 <= temp <= 70:
        active_led = "White"

    if active_led != "None":
        leds[active_led].on()
        np_set(scale(PIXEL_COLORS[active_led]))
    else:
        np_set((0, 0, 0))

# ------------------ HEATER ------------------
def heater_control(temp):
    global heater_on, last_temp_change

    if temp <= 15 and not heater_on:
        heater.on()
        heater_on = True
        last_temp_change = time.time()

    elif temp >= 30 and heater_on:
        heater.off()
        heater_on = False
        last_temp_change = time.time()

def heater_fail_check(temp):
    global last_ext_temp, last_temp_change, heater_on
    if heater_on:
        if last_ext_temp is not None and abs(temp - last_ext_temp) < 0.2:
            if time.time() - last_temp_change > FAIL_TIMEOUT:
                heater.off()
                heater_on = False
        else:
            last_temp_change = time.time()
    last_ext_temp = temp

# ------------------ ALARM ------------------
def alarm_update(temp):
    global alarm_latched, alarm_acknowledged, alarm_rearm
    global alarm_flash_state, last_flash_time

    if not alarm_enabled:
        return False

    # ---- Trigger ----
    out_of_range = temp <= 10 or temp >= 65
    if not out_of_range:
        alarm_rearm = True
    elif alarm_rearm and not alarm_latched:
        alarm_latched = True
        alarm_acknowledged = False
        alarm_rearm = False
        print("ALARM TRIGGERED:", temp)

    # ---- Not active ----
    if not alarm_latched or alarm_acknowledged:
        return False

    # ---- Flash timing ----
    now = time.ticks_ms()
    if time.ticks_diff(now, last_flash_time) < FLASH_INTERVAL:
        return True

    last_flash_time = now
    alarm_flash_state = not alarm_flash_state
    np_set(ALARM_BRIGHT if alarm_flash_state else ALARM_DIM)
    return True

def acknowledge():
    global alarm_acknowledged, alarm_latched
    alarm_acknowledged = True
    alarm_latched = False
    update_leds(external_temp)

# ------------------ NEOPIXEL WATCHDOG ------------------
def reset_neopixel():
    global np, neo_reset_count, last_neo_update
    last_neo_update = time.ticks_ms()      # don't retry on every loop pass
    if neo_reset_count >= NEO_MAX_RESETS:
        return
    neo_reset_count += 1
    try:
        np = make_np()
        print("NeoPixel reset:", neo_reset_count)
    except Exception as e:
        print("NeoPixel reset failed:", e)

def neopixel_watchdog():
    if time.ticks_diff(time.ticks_ms(), last_neo_update) > NEO_TIMEOUT:
        reset_neopixel()

# =====================================================
# WEB SERVER
# =====================================================
# The page is static: no string formatting, no reloads. The browser polls
# /data every 2 s and /hist every 10 s and updates the DOM in place.
PAGE = b"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pico W Monitor</title>
<style>
body{background:#111;color:#eee;font-family:Arial,sans-serif;margin:0;padding:10px;text-align:center}
.card{background:#1c1c1c;padding:20px;border-radius:16px;max-width:480px;margin:auto}
.value{font-size:20px;margin:6px 0}
.small{font-size:13px;color:#999;margin-top:6px}
.dot{display:inline-block;width:16px;height:16px;border-radius:50%;margin-left:8px;background:#444;vertical-align:middle}
.alarm{color:#ff3333;font-weight:bold}
button{padding:12px 20px;font-size:16px;border:none;border-radius:10px;background:#00ff88;color:#000;font-weight:bold;margin-top:10px}
svg{width:100%;height:200px;background:#000;border-radius:12px;margin-top:15px}
polyline{fill:none;stroke-width:2;vector-effect:non-scaling-stroke}
</style></head>
<body><div class="card">
<h2>Pico W Monitor</h2>
<div class="value"><b>Internal:</b> <span id="i">--</span> C</div>
<div class="value"><b>External:</b> <span id="e">--</span> C</div>
<div class="value"><b>Heater:</b> <span id="h">--</span></div>
<div class="value"><b>LED:</b> <span id="l">--</span><span class="dot" id="dot"></span></div>
<div class="value"><b>Alarm:</b> <span id="a">--</span></div>
<button onclick="ack()">ACKNOWLEDGE</button>
<svg id="g" viewBox="0 0 300 100" preserveAspectRatio="none"></svg>
<div class="small" id="rng">Green: external / Blue: internal</div>
<div class="value"><b>Uptime:</b> <span id="u">--</span></div>
<div class="value"><b>Reset:</b> <span id="r">--</span></div>
<div class="small" id="st"></div>
</div>
<script>
var LED={Blue:"#3399ff",Green:"#00ff88",Yellow:"#ffcc00",Red:"#ff3333",White:"#ffffff",None:"#444444"};
function $(id){return document.getElementById(id);}
function get(url){
  var c=new AbortController();
  var t=setTimeout(function(){c.abort();},4000);
  return fetch(url,{signal:c.signal,cache:"no-store"})
    .then(function(r){return r.json();})
    .finally(function(){clearTimeout(t);});
}
var busy=false, hbusy=false;
function poll(){
  if(busy) return;
  busy=true;
  get("/data").then(function(d){
    $("i").textContent=d.i.toFixed(2);
    $("e").textContent=d.e.toFixed(2);
    $("h").textContent=d.h?"ON":"OFF";
    $("l").textContent=d.l;
    $("dot").style.background=LED[d.l]||LED.None;
    $("a").textContent=d.a?"ACTIVE":"NORMAL";
    $("a").className=d.a?"alarm":"";
    $("u").textContent=d.u;
    $("r").textContent=d.r;
    $("st").textContent="";
  }).catch(function(){
    $("st").textContent="connection lost - retrying...";
  }).finally(function(){busy=false;});
}
function draw(ext,inn){
  if(ext.length<2) return;
  var all=ext.concat(inn), min=Math.min.apply(null,all), max=Math.max.apply(null,all);
  if(max-min<1){max=min+1;}
  function line(d){
    var p="";
    for(var k=0;k<d.length;k++){
      p+=(k/(d.length-1)*300).toFixed(1)+","+(100-(d[k]-min)/(max-min)*100).toFixed(1)+" ";
    }
    return p;
  }
  $("g").innerHTML='<polyline stroke="#00ff88" points="'+line(ext)+'"/><polyline stroke="#3399ff" points="'+line(inn)+'"/>';
  $("rng").textContent="Green: external / Blue: internal | "+min.toFixed(1)+" to "+max.toFixed(1)+" C";
}
function loadHist(){
  if(hbusy) return;
  hbusy=true;
  get("/hist").then(function(d){draw(d.ext,d.int);}).catch(function(){}).finally(function(){hbusy=false;});
}
function ack(){fetch("/ack",{cache:"no-store"}).catch(function(){}).finally(poll);}
poll();
setTimeout(loadHist,500);
setInterval(poll,2000);
setInterval(loadHist,10000);
</script></body></html>
"""

def json_data():
    return ('{"i":%.2f,"e":%.2f,"h":%d,"a":%d,"l":"%s","u":"%s","r":"%s"}' % (
        internal_temp,
        external_temp,
        1 if heater_on else 0,
        1 if (alarm_latched and not alarm_acknowledged) else 0,
        active_led,
        get_uptime(),
        reset_cause,
    )).encode()

def history_json():
    # Build the whole body in memory so it goes out in ONE write
    n = history_count
    start = (history_index - n) % HISTORY_SIZE
    ext = ",".join(["%.2f" % history_ext[(start + k) % HISTORY_SIZE] for k in range(n)])
    inn = ",".join(["%.2f" % history_int[(start + k) % HISTORY_SIZE] for k in range(n)])
    return ('{"ext":[%s],"int":[%s]}' % (ext, inn)).encode()

def send_response(conn, ctype, body, status="200 OK"):
    head = ("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n") % (status, ctype, len(body))
    conn.write(head.encode() + body)      # write() keeps going until everything is sent

def read_request(conn):
    """Read the whole header block (up to the blank line).

    Closing a socket that still has unread data makes lwIP send a TCP RST
    instead of a normal close, and browsers then throw the response away
    ("didn't send any data"). Real browser requests are often >512 bytes.
    """
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 4096:
        chunk = conn.recv(512)
        if not chunk:
            break
        data += chunk
    return data

def handle_client():
    """Serve at most one waiting connection. Returns False if none was waiting."""
    try:
        conn, _ = srv.accept()
    except OSError:
        return False
    try:
        conn.settimeout(0.5)
        req = read_request(conn)
        if req:
            # Only look at the request line ("GET /hist HTTP/1.1"), never the
            # headers: a Referer header can contain anything, and decoding a
            # truncated 512-byte chunk can raise UnicodeError.
            parts = req.split(b"\r\n", 1)[0].split(b" ")
            path = parts[1].split(b"?")[0] if len(parts) > 1 else b"/"
            if not use_wdt:
                print("HTTP", path)        # debug boot only

            if path == b"/data":
                send_response(conn, "application/json", json_data())
            elif path == b"/hist":
                send_response(conn, "application/json", history_json())
            elif path == b"/ack":
                acknowledge()
                send_response(conn, "application/json", b'{"ok":1}')
            elif path == b"/":
                send_response(conn, "text/html", PAGE)
            else:
                send_response(conn, "text/plain", b"not found", "404 Not Found")
    except Exception as e:
        print("client error:", e)
    finally:
        # ALWAYS close, on every path (the original leaked /data, /hist and timeouts)
        try:
            conn.close()
        except OSError:
            pass
    return True

# ------------------ WATCHDOG (started last, right before the loop) ------------------
class _NoWDT:
    def feed(self):
        pass

if use_wdt:
    wdt = machine.WDT(timeout=WDT_TIMEOUT)
    print("Watchdog ENABLED (%d ms)" % WDT_TIMEOUT)
else:
    wdt = _NoWDT()
    print("DEBUG MODE: watchdog OFF")

# ------------------ MAIN LOOP ------------------
while True:
    wdt.feed()
    try:
        daily_reboot_check()
        neopixel_watchdog()

        # ---- temperature state machine ----
        new_temp = read_external_temp_nonblocking()
        internal_temp = read_internal_temp()

        if new_temp is not None:
            external_temp = new_temp
            start_temp_conversion()          # queue the next conversion right away

            update_leds(external_temp)
            heater_control(external_temp)
            heater_fail_check(external_temp)
            alarm_update(external_temp)

            history_ext[history_index] = external_temp
            history_int[history_index] = internal_temp
            history_index = (history_index + 1) % HISTORY_SIZE
            if history_count < HISTORY_SIZE:
                history_count += 1

        if time.ticks_diff(time.ticks_ms(), last_gc) > GC_INTERVAL:
            gc.collect()
            last_gc = time.ticks_ms()

        # ---- web server: drain up to 4 waiting connections per pass ----
        for _ in range(4):
            if not handle_client():
                break
            wdt.feed()

    except Exception as e:
        # Log and keep running; a real hang is still caught by the watchdog.
        # (KeyboardInterrupt is not an Exception, so Ctrl-C still works.)
        print("Loop error:", e)
        time.sleep_ms(100)
