"""
HTTP server, ktery na GET /play vybere nahodny WAV soubor z adresare
a pomoci ffmpeg ho odvysila jako RTP multicast do reproduktoru
Grandstream GSC3518HS.

Reproduktor musi mit v nastaveni (Multicast Listening) nastavenou stejnou
multicast adresu a port jako MULTICAST_ADDR/MULTICAST_PORT. Kodek (CODEC)
pozna reproduktor z RTP payload type, u Opusu musi OPUS_PAYLOAD_TYPE sedet
s "Opus Payload Type" v reproduktoru.

Pouziti:
    python reproduktor.py
    (nebo v kontejneru, viz Containerfile a reproduktor.container)
    curl http://localhost:8080/play
    curl "http://localhost:8080/play?volume=-12dB"
    curl http://localhost:8080/status
    curl http://localhost:8080/stop

Konfigurace pres promenne prostredi (viz CONFIG nize).
"""

import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CONFIG = {
    # Adresar s WAV soubory
    "SOUNDS_DIR": os.environ.get("SOUNDS_DIR", str(Path(__file__).parent / "sounds")),
    # Multicast adresa a port, na ktere posloucha GSC3518HS
    "MULTICAST_ADDR": os.environ.get("MULTICAST_ADDR", "239.1.1.1"),
    "MULTICAST_PORT": int(os.environ.get("MULTICAST_PORT", "5000")),
    # IP adresa lokalniho sitoveho rozhrani, ze ktereho se ma multicast posilat
    # (dulezite na Windows / pri vice sitovkach). Prazdne = vychozi rozhrani.
    "LOCAL_ADDR": os.environ.get("LOCAL_ADDR", ""),
    "TTL": int(os.environ.get("TTL", "4")),
    # OPUS (fullband 48 kHz), G722 (wideband 16 kHz), PCMU / PCMA (G.711, 8 kHz)
    "CODEC": os.environ.get("CODEC", "OPUS").upper(),
    # Musi sedet s "Opus Payload Type" v reproduktoru (Account -> Codec Settings, vychozi 123)
    "OPUS_PAYLOAD_TYPE": int(os.environ.get("OPUS_PAYLOAD_TYPE", "123")),
    "OPUS_BITRATE": os.environ.get("OPUS_BITRATE", "64k"),
    # Hlasitost pro ffmpeg filtr volume: nasobek (0.3) nebo decibely (-10dB)
    "VOLUME": os.environ.get("VOLUME", "1.0"),
    "FFMPEG": os.environ.get("FFMPEG", "ffmpeg"),
    "HTTP_HOST": os.environ.get("HTTP_HOST", "0.0.0.0"),
    "HTTP_PORT": int(os.environ.get("HTTP_PORT", "8080")),
}


def codec_args(name):
    """Vrati (ffmpeg argumenty pro kodovani, max. velikost RTP paketu)."""
    if name == "OPUS":
        return [
            "-ar", "48000",
            "-acodec", "libopus",
            "-b:a", CONFIG["OPUS_BITRATE"],
            "-application", "audio",
            "-frame_duration", "20",
            "-payload_type", str(CONFIG["OPUS_PAYLOAD_TYPE"]),
        ], 1300
    # 20 ms na paket = 160 B audia + 12 B RTP hlavicka (u G.722 64 kbit/s i G.711)
    if name == "G722":
        return ["-ar", "16000", "-acodec", "g722", "-payload_type", "9"], 172
    if name == "PCMU":
        return ["-ar", "8000", "-acodec", "pcm_mulaw", "-payload_type", "0"], 172
    if name == "PCMA":
        return ["-ar", "8000", "-acodec", "pcm_alaw", "-payload_type", "8"], 172
    raise ValueError(name)


CODECS = ("OPUS", "G722", "PCMU", "PCMA")

# Jen cislo nebo cislo s dB, aby se pres ?volume= nedal podstrcit jiny ffmpeg filtr
VOLUME_RE = re.compile(r"^-?\d+(\.\d+)?(dB)?$")


def valid_volume(value):
    return bool(VOLUME_RE.match(value))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reproduktor")


class Player:
    """Hlida, aby v jednu chvili hral jen jeden soubor."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._current = None

    def pick_random_wav(self):
        sounds_dir = Path(CONFIG["SOUNDS_DIR"])
        files = [p for p in sounds_dir.iterdir() if p.is_file() and p.suffix.lower() == ".wav"]
        if not files:
            raise FileNotFoundError(f"V adresari {sounds_dir} nejsou zadne .wav soubory")
        return random.choice(files)

    def build_command(self, wav, volume):
        enc_args, pkt_size = codec_args(CONFIG["CODEC"])
        url = (f"rtp://{CONFIG['MULTICAST_ADDR']}:{CONFIG['MULTICAST_PORT']}"
               f"?ttl={CONFIG['TTL']}&pkt_size={pkt_size}")
        if CONFIG["LOCAL_ADDR"]:
            url += f"&localaddr={CONFIG['LOCAL_ADDR']}"
        return [
            CONFIG["FFMPEG"],
            "-hide_banner", "-loglevel", "error",
            "-re",                      # posilat v realnem case
            "-i", str(wav),
            "-vn",
            "-af", f"volume={volume}",
            "-ac", "1",                 # mono
            *enc_args,
            "-f", "rtp",
            url,
        ]

    def play(self, volume):
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return None
            wav = self.pick_random_wav()
            cmd = self.build_command(wav, volume)
            log.info("Prehravam %s (hlasitost %s) -> %s:%s",
                     wav.name, volume, CONFIG["MULTICAST_ADDR"], CONFIG["MULTICAST_PORT"])
            # stdout s SDP nepotrebujeme
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            self._current = wav.name
            threading.Thread(target=self._wait, args=(self._proc, wav.name), daemon=True).start()
            return wav.name

    def _wait(self, proc, name):
        code = proc.wait()
        if code == 0:
            log.info("Dohrano: %s", name)
        else:
            log.warning("ffmpeg skoncil s kodem %s (%s)", code, name)
        with self._lock:
            if self._proc is proc:
                self._proc = None
                self._current = None

    def stop(self):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return None
            self._proc.terminate()
            return self._current

    def status(self):
        with self._lock:
            playing = self._proc is not None and self._proc.poll() is None
            return {"playing": playing, "file": self._current if playing else None}


player = Player()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        try:
            if path == "/play":
                volume = parse_qs(url.query).get("volume", [CONFIG["VOLUME"]])[0]
                if not valid_volume(volume):
                    self._send(400, {"error": f"Neplatna hlasitost {volume!r}, pouzij napr. 0.3 nebo -10dB"})
                    return
                name = player.play(volume)
                if name is None:
                    self._send(409, {"error": "Uz se neco prehrava", **player.status()})
                else:
                    self._send(200, {"playing": True, "file": name, "volume": volume})
            elif path == "/stop":
                name = player.stop()
                self._send(200, {"stopped": name})
            elif path == "/status":
                self._send(200, player.status())
            else:
                self._send(404, {"error": "Nenalezeno", "endpoints": ["/play", "/stop", "/status"]})
        except FileNotFoundError as e:
            self._send(500, {"error": str(e)})
        except Exception as e:
            log.exception("Chyba pri zpracovani pozadavku")
            self._send(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)


def main():
    if CONFIG["CODEC"] not in CODECS:
        raise SystemExit(f"Nepodporovany CODEC {CONFIG['CODEC']}, pouzij {', '.join(CODECS)}")
    if not valid_volume(CONFIG["VOLUME"]):
        raise SystemExit(f"Neplatna VOLUME {CONFIG['VOLUME']!r}, pouzij napr. 0.3 nebo -10dB")
    if shutil.which(CONFIG["FFMPEG"]) is None:
        raise SystemExit(f"ffmpeg nenalezen ({CONFIG['FFMPEG']}), nastav promennou FFMPEG nebo ho pridej do PATH")
    Path(CONFIG["SOUNDS_DIR"]).mkdir(parents=True, exist_ok=True)

    # V kontejneru bezi skript jako PID 1, ktery SIGTERM bez handleru ignoruje
    # (podman stop by pak cekal 10 s na SIGKILL)
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)

    server = ThreadingHTTPServer((CONFIG["HTTP_HOST"], CONFIG["HTTP_PORT"]), Handler)
    log.info("Posloucham na http://%s:%s (zvuky: %s, multicast %s:%s, %s)",
             CONFIG["HTTP_HOST"], CONFIG["HTTP_PORT"], CONFIG["SOUNDS_DIR"],
             CONFIG["MULTICAST_ADDR"], CONFIG["MULTICAST_PORT"], CONFIG["CODEC"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        player.stop()
        server.server_close()


if __name__ == "__main__":
    main()
