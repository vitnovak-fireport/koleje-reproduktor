# Kojele – reproduktor

> Privátní projekt pro obsluhu zahradní železnice u mě doma na zahradě.

HTTP služba, která po zavolání `GET /play` vybere náhodný WAV soubor a pomocí
ffmpeg ho pošle jako RTP multicast (výchozí kodek Opus) do reproduktoru **Grandstream GSC3518HS**.

Nejde o SIP hovor – reproduktor jen poslouchá multicastovou adresu a stream okamžitě přehraje.

## Soubory

| Soubor | Popis |
|---|---|
| `reproduktor.py` | HTTP server + spouštění ffmpeg (jen standardní knihovna Pythonu) |
| `Containerfile` | Obraz pro podman (Debian trixie-slim + python3 + ffmpeg) |
| `reproduktor.container` | Podman Quadlet – automatický start přes systemd |
| `.containerignore` | Co nekopírovat do obrazu |

## Endpointy

| Endpoint | Co dělá |
|---|---|
| `GET /play` | Přehraje náhodný `.wav` ze `SOUNDS_DIR`. Když už něco hraje, vrátí `409`. |
| `GET /play?volume=-12dB` | Totéž s jinou hlasitostí než `VOLUME` (viz [Hlasitost](#hlasitost)). |
| `GET /announce?file=zastavime` | Přehraje konkrétní hlášku ze `SPECIFIC_SOUNDS_DIR` (přípona `.wav` je nepovinná). Když už něco hraje, přeruší to. Bere i `volume=`. Neexistující soubor = `404`. |
| `GET /stop` | Zastaví přehrávání. |
| `GET /status` | Vrátí `{"playing": true/false, "file": "..."}`. |

```bash
curl http://<ip-pi>:8080/play
```

```bash
curl "http://<ip-pi>:8080/announce?file=zastavka-na-znameni"
```

## Konfigurace (proměnné prostředí)

| Proměnná | Výchozí | Popis |
|---|---|---|
| `SOUNDS_DIR` | `./sounds` (v kontejneru `/sounds`) | Adresář s WAV soubory |
| `SPECIFIC_SOUNDS_DIR` | `./sounds-specific` (v kontejneru `/app/sounds-specific`) | Adresář s konkrétními hláškami pro `/announce` – je součástí repozitáře i obrazu |
| `MULTICAST_ADDR` | `239.1.1.1` | Multicast adresa nastavená v reproduktoru |
| `MULTICAST_PORT` | `5000` | Port nastavený v reproduktoru |
| `CODEC` | `OPUS` | `OPUS`, `G722`, `PCMU` nebo `PCMA` – viz [Kodeky](#kodeky) |
| `OPUS_PAYLOAD_TYPE` | `123` | Musí sedět s „Opus Payload Type“ v reproduktoru |
| `OPUS_BITRATE` | `64k` | Datový tok Opusu (mono) |
| `VOLUME` | `0.1` | Výchozí hlasitost – násobek (`0.3`) nebo decibely (`-10dB`) |
| `NORMALIZE` | `1` | Normalizace hlasitosti všech souborů (`0` = vypnuto) |
| `LOUDNESS` | `-16` | Cílová hlasitost normalizace v LUFS |
| `TRUE_PEAK` | `-1.5` | Maximální špička po normalizaci v dBTP |
| `LEAD_IN_MS` | `1000` | Ticho před začátkem každého souboru v ms, aby reproduktor stihl chytit stream a neusekl začátek (`0` = vypnuto) |
| `LOCAL_ADDR` | – | IP rozhraní, ze kterého multicast posílat (při více sítovkách) |
| `TTL` | `4` | TTL multicastových paketů |
| `HTTP_HOST` | `0.0.0.0` | Adresa HTTP serveru |
| `HTTP_PORT` | `8080` | Port HTTP serveru |
| `FFMPEG` | `ffmpeg` | Cesta k ffmpeg |

## Kodeky

Od nejkvalitnějšího:

| `CODEC` | Pásmo | Vzorkování | RTP payload type |
|---|---|---|---|
| `OPUS` | fullband (do 20 kHz) | 48 kHz | dynamický, výchozí `123` |
| `G722` | wideband (do 7 kHz) | 16 kHz | `9` |
| `PCMU` / `PCMA` | narrowband (do 3,4 kHz) | 8 kHz | `0` / `8` |

Reproduktor podle datasheetu umí G.711, G.722 i Opus, ale Grandstream nikde
neuvádí, které z nich přijímá v **multicastu**. Postup: zkusit `OPUS`, když
reproduktor nehraje, `G722`, a nakonec `PCMU`, který funguje vždy.

U Opusu musí `OPUS_PAYLOAD_TYPE` sedět s nastavením reproduktoru
**Account → Codec Settings → Opus Payload Type** (výchozí 123).

Kvalitu omezují i samotné WAV soubory – nahrávka v 8 kHz bude znít stejně
s jakýmkoli kodekem.

Rychlé přepnutí kodeku při ručním testu:

```bash
podman run --rm --net=host -v ~/sounds:/sounds:ro,Z -e CODEC=G722 reproduktor
```

## Hlasitost

Zvuk se ztlumí už při kódování filtrem ffmpeg `volume`, takže to funguje
i když je reproduktor nastavený na minimum a pořád hraje moc nahlas.

| Hodnota | Význam |
|---|---|
| `1.0` | beze změny |
| `0.5` nebo `-6dB` | zhruba poloviční amplituda |
| `0.25` nebo `-12dB` | výrazně tišší |
| `0.1` nebo `-20dB` | hodně potichu |

Výchozí hodnota se nastaví proměnnou `VOLUME` (v `reproduktor.container`),
pro jedno přehrání ji lze přebít parametrem:

```bash
curl "http://<ip-pi>:8080/play?volume=-12dB"
```

Nejjednodušší je zkoušet různé `?volume=` a vybranou hodnotu pak zapsat do `VOLUME`.
Po změně `reproduktor.container`:

```bash
cp reproduktor.container ~/.config/containers/systemd/ && systemctl --user daemon-reload && systemctl --user restart reproduktor
```

## Normalizace

Aby všechny zvuky hrály stejně nahlas, používá se filtr ffmpeg `loudnorm`
(EBU R128) ve dvou průchodech:

1. Při prvním přehrání se soubor rychle změří (ne v reálném čase, na Pi
   zabere u krátkého zvuku zlomek sekundy až pár sekund).
2. Při přehrání se použije přesná lineární korekce na `LOUDNESS` LUFS – zvuk se
   jen zesílí/zeslabí, bez „pumpování“.

Měření se drží v paměti podle cesty a času změny souboru, takže se dělá jen
jednou (po restartu služby znovu). Když měření selže, použije se jednoprůchodový
dynamický `loudnorm`.

Pořadí filtrů je `loudnorm` → `volume`, takže `VOLUME` / `?volume=` pak ztlumí
už vyrovnaný zvuk.

`LOUDNESS`: `-16` je běžná hlasitost hlášení, `-23` je úroveň TV vysílání (tišší).
Hodně tichý zvuk se při normalizaci zesílí – pokud hraje celkově moc nahlas,
stačí snížit `LOUDNESS` nebo `VOLUME`.

Ručně změřit soubor:

```bash
ffmpeg -hide_banner -nostats -i test.wav -af loudnorm=I=-16:TP=-1.5:LRA=20:print_format=json -f null -
```

## Nastavení reproduktoru GSC3518HS

V GDMS / webovém rozhraní: **Device Settings → Multicast**

- **Multicast Listening**
  - Enable Multicast Listening: ✅
  - Řádek 1 → Address: `239.1.1.1:5000` (musí sedět s `MULTICAST_ADDR:MULTICAST_PORT`)
  - Label: libovolný, např. „Hlášení“
  - Application: **VoIP** (paging s prioritou; když by nehrálo, zkusit Audio)
  - Order = priorita, 1 je nejvyšší
- **Multicast Paging**
  - Paging Barge: `Disabled` = hlášení nepřeruší probíhající SIP hovor; `1` = kanál 1 hovor přeruší
  - Paging Priority Active: zapnuto
- **Save and Apply**

Kodek se v Multicast Listening nenastavuje, reproduktor ho pozná z RTP payload type.

## Nasazení na Raspberry Pi (podman)

### 1. Příprava

Zkopírovat na Pi `reproduktor.py`, `Containerfile`, `reproduktor.container`
a WAV soubory dát do `~/sounds`:

```bash
mkdir -p ~/sounds
```

```bash
chmod 644 ~/sounds/*.wav
```

### 2. Build obrazu

```bash
podman build -t reproduktor .
```

### 3. Ruční test

```bash
podman run --rm --net=host -v ~/sounds:/sounds:ro,Z reproduktor
```

S vlastní konfigurací:

```bash
podman run --rm --net=host -v ~/sounds:/sounds:ro,Z -e LOCAL_ADDR=192.168.1.50 reproduktor
```

### 4. Trvalý běh přes systemd (Quadlet)

```bash
mkdir -p ~/.config/containers/systemd
```

```bash
cp reproduktor.container ~/.config/containers/systemd/
```

```bash
systemctl --user daemon-reload
```

```bash
systemctl --user start reproduktor
```

Aby služba běžela i bez přihlášení a po rebootu:

```bash
sudo loginctl enable-linger $USER
```

Quadlet vyžaduje Podman ≥ 4.4.

### Správa služby

```bash
systemctl --user status reproduktor
```

```bash
systemctl --user restart reproduktor
```

```bash
journalctl --user -u reproduktor -f
```

### Aktualizace po změně skriptu

```bash
podman build -t reproduktor .
```

```bash
systemctl --user restart reproduktor
```

Po změně `reproduktor.container` nejdřív znovu zkopírovat soubor a spustit `systemctl --user daemon-reload`.

## Lokální spuštění bez kontejneru

Potřebuje Python 3 a ffmpeg v PATH:

```bash
python3 reproduktor.py
```

## Ruční test ffmpeg (bez Pythonu)

Opus:

```bash
ffmpeg -re -i test.wav -vn -af volume=-12dB -ac 1 -ar 48000 -acodec libopus -b:a 64k -application audio -frame_duration 20 -payload_type 123 -f rtp "rtp://239.1.1.1:5000?ttl=4&pkt_size=1300"
```

G.722:

```bash
ffmpeg -re -i test.wav -vn -af volume=-12dB -ac 1 -ar 16000 -acodec g722 -payload_type 9 -f rtp "rtp://239.1.1.1:5000?ttl=4&pkt_size=172"
```

G.711 µ-law:

```bash
ffmpeg -re -i test.wav -vn -af volume=-12dB -ac 1 -ar 8000 -acodec pcm_mulaw -payload_type 0 -f rtp "rtp://239.1.1.1:5000?ttl=4&pkt_size=172"
```

## Když reproduktor nehraje

- Pi a reproduktor musí být ve **stejné síti / VLAN** – multicast běžně neprojde routerem.
- Má-li Pi víc rozhraní (Wi-Fi + ethernet), nastavit `LOCAL_ADDR` na IP Pi v síti reproduktoru.
- Pakety zkontrolovat přímo na Pi:
  ```bash
  sudo tcpdump -ni any host 239.1.1.1
  ```
- Switch s **IGMP snoopingem** bez IGMP querieru může multicast po chvíli zahazovat – zapnout querier nebo snooping vypnout.
- Ověřit, že se nastavení z GDMS opravdu propsalo do reproduktoru (webové rozhraní reproduktoru).
- WAV soubory musí být čitelné pro všechny (`chmod 644`), kontejner běží pod neprivilegovaným uživatelem.
