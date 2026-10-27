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
    curl "http://localhost:8080/announce?file=zastavime"
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
    # Adresar s konkretnimi hlaskami pro /announce?file=
    "SPECIFIC_SOUNDS_DIR": os.environ.get("SPECIFIC_SOUNDS_DIR", str(Path(__file__).parent / "sounds-specific")),
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
    "VOLUME": os.environ.get("VOLUME", "0.1"),
    # Normalizace hlasitosti (EBU R128 loudnorm), aby vsechny soubory hraly stejne nahlas.
    # VOLUME se aplikuje az po normalizaci.
    "NORMALIZE": os.environ.get("NORMALIZE", "1").lower() in ("1", "true", "yes", "on"),
    # Cilova hlasitost v LUFS (-16 = bezna hlasitost hlaseni/podcastu, -23 = vysilani TV)
    "LOUDNESS": float(os.environ.get("LOUDNESS", "-16")),
    # Maximalni spicka (true peak) v dBTP
    "TRUE_PEAK": float(os.environ.get("TRUE_PEAK", "-1.5")),
    # Ticho pred zacatkem kazdeho souboru v ms. Reproduktoru chvili trva, nez se
    # na novy stream chyti, a bez nej by kratke hlasky spolkl cele.
    "LEAD_IN_MS": int(os.environ.get("LEAD_IN_MS", "1000")),
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


def loudnorm_target():
    # LRA=20 (maximum starsich ffmpeg): linear=true funguje jen kdyz zmerene LRA
    # nepresahne cil, jinak loudnorm prepne na dynamicky rezim
    return f"loudnorm=I={CONFIG['LOUDNESS']}:TP={CONFIG['TRUE_PEAK']}:LRA=20"


class Loudness:
    """
    Dvoupruchodova normalizace: soubor se nejdriv zmeri (rychle, ne v realnem case)
    a pri prehravani se pak pouzije presna linearni korekce. Mereni se cachuje
    podle cesty a casu zmeny souboru, takze se dela jen pri prvnim prehrani.
    """

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def filter_for(self, wav):
        key = (str(wav), wav.stat().st_mtime_ns)
        with self._lock:
            measured = self._cache.get(key)
        if measured is None:
            try:
                measured = self._measure(wav)
            except Exception as e:
                # Fallback: jednopruchodovy (dynamicky) loudnorm primo pri prehravani
                log.warning("Mereni hlasitosti %s selhalo (%s), pouziji dynamicky loudnorm", wav.name, e)
                return loudnorm_target()
            with self._lock:
                self._cache[key] = measured
        return (f"{loudnorm_target()}"
                f":measured_I={measured['input_i']}"
                f":measured_TP={measured['input_tp']}"
                f":measured_LRA={measured['input_lra']}"
                f":measured_thresh={measured['input_thresh']}"
                f":offset={measured['target_offset']}"
                f":linear=true")

    def _measure(self, wav):
        cmd = [
            CONFIG["FFMPEG"], "-hide_banner", "-nostats",
            "-i", str(wav),
            "-vn", "-af", f"{loudnorm_target()}:print_format=json",
            "-f", "null", "-",
        ]
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg kod {result.returncode}")
        # loudnorm vypise JSON na konec stderr
        stderr = result.stderr
        start = stderr.rfind("{")
        end = stderr.rfind("}")
        if start == -1 or end < start:
            raise RuntimeError("ve vystupu ffmpeg chybi JSON")
        measured = json.loads(stderr[start:end + 1])
        log.info("Zmereno %s: %s LUFS, spicka %s dBTP",
                 wav.name, measured["input_i"], measured["input_tp"])
        return measured


loudness = Loudness()

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

    def find_specific_wav(self, name):
        """Najde hlasku podle nazvu (s priponou .wav i bez). Hleda se jen mezi soubory
        v adresari, takze pres nazev nejde sahnout mimo nej."""
        sounds_dir = Path(CONFIG["SPECIFIC_SOUNDS_DIR"])
        wanted = name.lower()
        if not wanted.endswith(".wav"):
            wanted += ".wav"
        if sounds_dir.is_dir():
            for p in sounds_dir.iterdir():
                if p.is_file() and p.name.lower() == wanted:
                    return p
        raise FileNotFoundError(f"Hlaska {name!r} v adresari {sounds_dir} neexistuje")

    def build_command(self, wav, volume, norm_filter=None):
        enc_args, pkt_size = codec_args(CONFIG["CODEC"])
        filters = [norm_filter] if norm_filter else []
        filters.append(f"volume={volume}")
        if CONFIG["LEAD_IN_MS"] > 0:
            filters.append(f"adelay={CONFIG['LEAD_IN_MS']}:all=1")
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
            "-af", ",".join(filters),
            "-ac", "1",                 # mono
            *enc_args,
            "-f", "rtp",
            url,
        ]

    def is_playing(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def play(self, volume, wav=None, preempt=False):
        """Bez wav vybere nahodny soubor. Kdyz uz neco hraje, vrati None,
        s preempt=True puvodni prehravani zastavi a pusti wav."""
        if not preempt and self.is_playing():
            return None
        if wav is None:
            wav = self.pick_random_wav()
        # Mereni muze par sekund trvat, proto mimo zamek (/status mezitim odpovida)
        norm_filter = loudness.filter_for(wav) if CONFIG["NORMALIZE"] else None
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                if not preempt:
                    return None
                log.info("Prerusuji %s kvuli %s", self._current, wav.name)
                self._terminate(self._proc)
            cmd = self.build_command(wav, volume, norm_filter)
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

    @staticmethod
    def _terminate(proc):
        # Pockat na konec, aby se do multicastu nemichaly dva streamy
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

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
            elif path == "/announce":
                query = parse_qs(url.query)
                name = query.get("file", [""])[0]
                volume = query.get("volume", [CONFIG["VOLUME"]])[0]
                if not name:
                    self._send(400, {"error": "Chybi parametr file, napr. /announce?file=zastavime"})
                    return
                if not valid_volume(volume):
                    self._send(400, {"error": f"Neplatna hlasitost {volume!r}, pouzij napr. 0.3 nebo -10dB"})
                    return
                try:
                    wav = player.find_specific_wav(name)
                except FileNotFoundError as e:
                    self._send(404, {"error": str(e)})
                    return
                player.play(volume, wav=wav, preempt=True)
                self._send(200, {"playing": True, "file": wav.name, "volume": volume})
            else:
                self._send(404, {"error": "Nenalezeno", "endpoints": ["/play", "/announce", "/stop", "/status"]})
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
