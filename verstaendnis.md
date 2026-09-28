# Verständnis: Aufnahme und Synchronisation

Dieses Dokument erklärt die vier Dateien `setup_nuc.sh`, `nucs.sh`, `record.py` und `sync_frames.py`: wie sie zusammenspielen, welche Funktionen sie enthalten und was die wichtigen Zeilen im Detail tun.

## 1. Überblick

Der Arduino Nano am Camserver erzeugt mit fester Frequenz (z.B. 30 Hz) Rechteckpulse. Diese liegen an Line0 aller sechs Kameras an. Jede steigende Flanke startet die Belichtung, alle Kameras belichten also zum selben Zeitpunkt. Jede Kamera hängt per USB3 an einem eigenen NUC. Auf jedem NUC läuft `record.py`. Es speichert jedes ankommende Bild zusammen mit der Uhrzeit des NUC. Die Uhren der NUCs sind per NTP synchronisiert.

Das Problem: Jeder NUC nummeriert seine Bilder unabhängig. Startet ein NUC etwas später oder verliert ein Bild, stimmt `000100.png` auf nucS nicht mehr mit `000100.png` auf nucN überein. Deshalb ordnet `sync_frames.py` die Bilder über die Zeitstempel einander zu und nummeriert sie neu. Danach gilt: gleiche Nummer = gleicher Triggerpuls.

```
Arduino ──Puls──> 6 Kameras ──USB──> 6 NUCs (record.py)
                                         │  ./nucs.sh fetch
                                         ▼
                                  Laptop: recordings/<name>/nucS, nucN, ...
                                         │  sync_frames.py
                                         ▼
                                  recordings/<name>/synced/
```

| Datei | läuft auf | Aufgabe |
|---|---|---|
| `nucs.sh` | Laptop | steuert alle NUCs gleichzeitig per SSH |
| `setup_nuc.sh` | NUC | richtet die Python-Umgebung `~/pose-rec/.venv` ein |
| `record.py` | NUC | Kamera konfigurieren, Bilder und Zeitstempel speichern |
| `sync_frames.py` | Laptop | Bilder aller Kameras per Zeitstempel zuordnen |

Die Zuordnung von NUC zu Kameraposition ist fest:

| IP | 192.168.0.12 | .13 | .14 | .15 | .16 | .17 |
|---|---|---|---|---|---|---|
| Ordner | nucS | nucN | nucNW | nucSW | nucNE | nucSE |

---

## 2. `setup_nuc.sh`

Das Skript erzeugt auf einem NUC eine virtuelle Python-Umgebung und installiert dort `ids_peak`, `numpy` und `opencv`. Rootrechte sind dafür nicht nötig. Es wird von `./nucs.sh deploy` auf allen NUCs aufgerufen.

```bash
set -e
```
Das Skript bricht ab, sobald ein Befehl einen Fehlercode ungleich 0 zurückgibt. Ohne diese Zeile würde es nach einem fehlgeschlagenen Schritt einfach weiterlaufen. Ausnahmen sind Befehle in `if`-Bedingungen und links von `||` oder `&&`. Deren Fehler sind ja gewollt abgefangen.

```bash
cd "$(dirname "$0")"
```
`$0` ist der Pfad, mit dem das Skript aufgerufen wurde (z.B. `pose-rec/setup_nuc.sh`). `dirname` schneidet den Dateinamen ab und liefert `pose-rec`. `$( ... )` ist eine *Command Substitution*: Bash führt den inneren Befehl aus und setzt dessen Ausgabe als Text ein. So arbeitet das Skript immer in seinem eigenen Ordner, egal von wo es gestartet wurde.

```bash
[ -x .venv/bin/python ] || python3 -m venv .venv
```
`[ -x datei ]` prüft, ob die Datei existiert und ausführbar ist. `A || B` bedeutet: B nur ausführen, wenn A fehlschlägt. Die venv wird also nur beim ersten Mal angelegt. Ein erneutes `deploy` ist dadurch harmlos. Man nennt das *idempotent*.

```bash
PY=.venv/bin/python
CHECK="from ids_peak import ids_peak, ids_peak_ipl_extension"
```
Alle Installationen laufen über das Python der venv, nicht über das System-Python. `CHECK` ist der Import, den `record.py` später braucht. Er dient als Test, ob die Installation funktioniert.

```bash
if ! $PY -c "$CHECK" 2>/dev/null; then
    for whl in $(find /usr /opt -name 'ids_peak*.whl' 2>/dev/null); do
        $PY -m pip install --quiet "$whl" || true
    done
fi
```
`python -c "..."` führt den Text als Python-Code aus. Klappt der Import, ist der Exit-Code 0, sonst 1. `!` kehrt das um. `2>/dev/null` leitet die Fehlerausgabe (Kanal 2, stderr) ins Nichts, damit die Import-Fehlermeldung nicht erscheint.

Nur wenn `ids_peak` fehlt, sucht `find` in `/usr` und `/opt` nach Wheel-Dateien, die mit dem IDS-peak-SDK mitgeliefert wurden. Diese passen garantiert zur installierten SDK-Version. `|| true` verhindert, dass `set -e` das Skript beendet, wenn ein Wheel nicht passt (z.B. für eine andere Python-Version). Dann wird einfach das nächste probiert.

```bash
$PY -c "$CHECK" 2>/dev/null || $PY -m pip install --quiet ids_peak ids_peak_ipl
```
Fehlt `ids_peak` immer noch, wird es aus dem Internet (PyPI) installiert.

```bash
$PY -c "$CHECK; import cv2; print('setup ok')"
```
Abschlusstest. Wenn hier `setup ok` erscheint, kann `record.py` laufen.

---

## 3. `nucs.sh`

Das Steuerskript auf dem Laptop. Es hat drei Befehle:

```
./nucs.sh deploy
./nucs.sh record <name> <sekunden> <belichtung_ms>
./nucs.sh fetch <name>
```

### 3.1 Einstellungen

```bash
set -u
```
Die Verwendung einer nicht gesetzten Variable ist ein Fehler. Ein Tippfehler wie `$nmae` fällt so sofort auf, statt still als leerer Text eingesetzt zu werden.

```bash
declare -A NUCS=([12]=S [13]=N [14]=NW [15]=SW [16]=NE [17]=SE)
```
`declare -A` legt ein *assoziatives Array* an, also eine Zuordnung Schlüssel → Wert (wie ein Python-Dict). `${NUCS[12]}` ergibt `S`. `${!NUCS[@]}` (mit Ausrufezeichen) liefert alle Schlüssel, also `12 13 14 ...`. Ohne `!` bekommt man die Werte. Die Reihenfolge der Schlüssel ist in Bash nicht festgelegt. Deshalb erscheinen die Ausgaben der NUCs nicht sortiert.

```bash
SSH_OPTS=(-i "$KEY" -o BatchMode=yes -o ConnectTimeout=5)
```
Ein normales Bash-Array mit den SSH-Optionen. `"${SSH_OPTS[@]}"` setzt jedes Element als eigenes Argument ein, auch wenn ein Element Leerzeichen enthält.
- `-i` gibt den privaten Schlüssel an, mit dem man sich ohne Passwort anmeldet.
- `BatchMode=yes` sorgt dafür, dass SSH nie nach einem Passwort fragt, sondern sofort mit Fehler abbricht. Sonst würden sechs parallele Passwortabfragen das Terminal blockieren.
- `ConnectTimeout=5` bricht nach 5 s ab, wenn ein NUC nicht erreichbar ist.

```bash
HERE="$(cd "$(dirname "$0")" && pwd)"
```
Der absolute Pfad des Ordners, in dem `nucs.sh` liegt. Dorthin werden die Aufnahmen geholt, unabhängig davon, in welchem Ordner man gerade steht.

### 3.2 Hilfsfunktionen

```bash
host() { echo "stud@192.168.0.$1"; }
```
`$1` ist das erste Argument der Funktion. `host 12` gibt `stud@192.168.0.12` aus. Mit `$(host "$ip")` wird diese Ausgabe als Text eingesetzt.

```bash
usage() { sed -n '2,6p' "$0"; exit 1; }
```
`sed -n '2,6p'` gibt nur die Zeilen 2 bis 6 einer Datei aus: `-n` unterdrückt die normale Ausgabe, `p` druckt den angegebenen Bereich. Hier ist die Datei das Skript selbst (`$0`), also genau der Hilfekommentar am Anfang. Die Hilfe muss so nur an einer Stelle gepflegt werden.

```bash
run_all() {
    for ip in "${!NUCS[@]}"; do
        ssh "${SSH_OPTS[@]}" "$(host "$ip")" "$1" 2>&1 | sed -u "s/^/[nuc${NUCS[$ip]}] /" &
    done
    wait
}
```
Das ist die zentrale Funktion. Sie führt den Befehl `$1` auf allen NUCs **gleichzeitig** aus:
- `ssh ... host "befehl"` führt `befehl` auf dem NUC aus. Die Ausgabe kommt auf dem Laptop an.
- `2>&1` leitet stderr in stdout um, damit auch Fehlermeldungen durch die Pipe gehen.
- `| sed -u "s/^/[nucS] /"` ersetzt den Zeilenanfang `^` durch `[nucS] `. Jede Zeile bekommt so ein Präfix, und man sieht, welcher NUC sie geschrieben hat. `-u` (unbuffered) gibt jede Zeile sofort weiter, statt zu sammeln.
- `&` am Ende startet die ganze Pipe im Hintergrund, und die Schleife läuft sofort zum nächsten NUC weiter.
- `wait` blockiert, bis **alle** Hintergrundprozesse der Shell beendet sind.

### 3.3 Befehlsauswahl

```bash
case "${1:-}" in
deploy) ... ;;
record) ... ;;
fetch)  ... ;;
*)      usage ;;
esac
```
`case` vergleicht das erste Argument mit den Mustern. `;;` beendet einen Zweig, `*` fängt alles andere ab. `${1:-}` heißt: `$1`, oder ein leerer Text, falls kein Argument übergeben wurde. Ohne `:-` würde `set -u` bei `./nucs.sh` ohne Argument einen Fehler werfen.

### 3.4 `deploy`

```bash
ssh ... "mkdir -p $REMOTE_DIR"
scp -q "${SSH_OPTS[@]}" "$HERE/record.py" "$HERE/setup_nuc.sh" "$(host "$ip"):$REMOTE_DIR/"
...
run_all "bash $REMOTE_DIR/setup_nuc.sh"
```
`mkdir -p` legt den Ordner an und ist still, falls er schon existiert. `scp` kopiert Dateien über SSH. Die Syntax `host:pfad` bezeichnet einen Pfad auf dem entfernten Rechner. Relative Pfade gelten dort ab dem Home-Verzeichnis. `-q` unterdrückt die Fortschrittsanzeige. Danach läuft `setup_nuc.sh` parallel auf allen NUCs.

Hinweis: `$REMOTE_DIR` steht in doppelten Anführungszeichen und wird deshalb schon **auf dem Laptop** eingesetzt. Beim NUC kommt der fertige Text `mkdir -p pose-rec` an. Das ist hier gewollt.

### 3.5 `record`

```bash
[ $# -eq 4 ] || usage
```
`$#` ist die Anzahl der Argumente. `record name 30 15` sind vier. Stimmt die Anzahl nicht, erscheint die Hilfe.

```bash
run_all "cd $REMOTE_DIR && exec .venv/bin/python -u record.py recordings/$name $3 $4"
```
Auf jedem NUC wird in `pose-rec` gewechselt und `record.py` gestartet.
- `&&` bedeutet: den zweiten Befehl nur ausführen, wenn der erste erfolgreich war.
- `exec` ersetzt die Shell, die SSH auf dem NUC startet, durch den Python-Prozess. Es bleibt kein Zwischen-Bash übrig, und das Signal von `pkill` (siehe unten) trifft direkt Python.
- `-u` schaltet die Ausgabepufferung von Python ab. Sonst würde Python bei einer Pipe (statt eines Terminals) die Ausgaben sammeln, und man sähe `recording 30 s` erst am Ende.

```bash
trap 'run_all "pkill -INT -f \"record[.]py recordings/$name \""' INT
```
Das ist der wichtigste Trick des Skripts. Er ist nötig, weil ein Strg+C auf dem Laptop die Programme auf den NUCs **nicht** erreicht. Die SSH-Verbindungen laufen im Hintergrund (`&`), und Hintergrundprozesse eines Skripts ignorieren SIGINT. Außerdem hat die SSH-Sitzung kein Terminal, über das das Signal weitergereicht werden könnte.

- `trap 'befehle' INT` legt fest, was die Shell bei SIGINT (Strg+C) ausführen soll, statt sich zu beenden.
- Der Befehl ist ein erneutes `run_all`, das auf jedem NUC `pkill` aufruft.
- `pkill -INT -f muster` schickt SIGINT an alle Prozesse, deren **komplette Kommandozeile** (`-f`) auf das Muster passt. Python wandelt SIGINT in eine `KeyboardInterrupt`-Exception um, und `record.py` beendet sich sauber.
- `record[.]py` ist ein regulärer Ausdruck. `[.]` passt genau auf einen Punkt. Der Grund für diese Schreibweise: Auch die Shell, die `pkill` startet, hat das Muster in ihrer eigenen Kommandozeile stehen. Dort steht aber wörtlich `record[.]py` (mit eckigen Klammern), und das passt nicht auf den regulären Ausdruck. So beendet `pkill` nicht versehentlich sich selbst oder seine Shell.
- Das Leerzeichen nach `$name` verhindert, dass Stop für `test` auch die Aufnahme `test2` trifft.

Der Ablauf nach Strg+C: Das erste `wait` in `run_all` wird unterbrochen, und die Trap läuft. Ihr eigenes `run_all` endet ebenfalls mit `wait`. Dieses wartet auf **alle** Hintergrundprozesse, also auch auf die ursprünglichen Aufnahme-Verbindungen. Deshalb erscheint auf dem Laptop noch `stopped early` und `done: N frames` von jedem NUC, bevor das Skript endet.

### 3.6 `fetch`

```bash
dest="$HERE/recordings/$2/nuc${NUCS[$ip]}"
mkdir -p "$dest"
ssh ... "tar -C $REMOTE_DIR/recordings/$2 -cf - ." | tar -C "$dest" -xf -
```
Kopiert die Aufnahme eines NUC nach `recordings/<name>/nucS` usw. Hier findet die Umbenennung von der IP zur Position statt.

Die Übertragung ist eine *tar-Pipe*:
- Auf dem NUC wechselt `tar -C ordner` in den Ordner, und `-c` erzeugt ein Archiv aus allem darin (`.`). `-f -` schreibt das Archiv nach stdout statt in eine Datei.
- SSH überträgt diesen Datenstrom zum Laptop.
- Dort liest `tar -xf -` das Archiv von stdin (`-`) und entpackt es mit `-C "$dest"` in den Zielordner.

Vorteile gegenüber `scp -r`: Tausende kleine Dateien gehen als ein einziger Datenstrom durch, was deutlich schneller ist. Außerdem braucht es auf den NUCs kein `rsync`.

---

## 4. `record.py`

Läuft auf jedem NUC. Aufruf (normalerweise über `nucs.sh`):

```
python record.py <out_dir> <dauer_s> <belichtung_ms>
```

Ergebnis:
```
<out_dir>/frames/000000.png, 000001.png, ...
<out_dir>/frames.csv          file, host_time_ns
```

### 4.1 Vor den Imports

```python
os.environ.setdefault("GENICAM_GENTL64_PATH", "/usr/lib/x86_64-linux-gnu/ids-peak/cti")
```
Das IDS-SDK findet die Kamera über einen *GenTL-Producer* (eine `.cti`-Datei, die den USB3-Treiber kapselt). Wo diese liegt, liest das SDK aus der Umgebungsvariable `GENICAM_GENTL64_PATH`. Der systemd-Service von peakcvbridge setzt sie, eine normale SSH-Sitzung aber nicht. Ohne diese Zeile würde keine Kamera gefunden. `setdefault` setzt den Wert nur, falls die Variable noch nicht existiert. Die Zeile muss **vor** `from ids_peak import ...` stehen, weil das SDK den Pfad beim Laden liest.

```python
NUM_BUFFERS = 16
NUM_WRITERS = 4
```
Die Anzahl der Bildpuffer im SDK und der Schreib-Threads (Details unten).

### 4.2 `execute(nm, name)`

```python
node = nm.FindNode(name)
node.Execute()
node.WaitUntilDone()
```
Die Kamera wird über *GenICam-Nodes* gesteuert: benannte Parameter wie `ExposureTime` oder Befehle wie `AcquisitionStart`. `nm` ist die *NodeMap*, also die Liste aller Nodes der Kamera. Befehls-Nodes werden mit `Execute()` ausgelöst. `WaitUntilDone()` wartet, bis die Kamera den Befehl fertig ausgeführt hat.

### 4.3 `open_camera(exposure_ms)`

```python
dm = ids_peak.DeviceManager.Instance()
dm.Update()
device = dm.Devices()[0].OpenDevice(ids_peak.DeviceAccessType_Control)
nm = device.RemoteDevice().NodeMaps()[0]
```
Der DeviceManager sucht alle angeschlossenen Kameras (`Update`). An jedem NUC hängt genau eine, also Index 0. `DeviceAccessType_Control` öffnet sie mit Schreibrechten. Ist die Kamera schon durch einen anderen Prozess geöffnet (z.B. ein verbundener peakcvbridge-Stream oder IDS peak Cockpit), schlägt das fehl. `RemoteDevice().NodeMaps()[0]` ist die NodeMap der Kamera selbst.

```python
nm.FindNode("UserSetSelector").SetCurrentEntry("Default")
execute(nm, "UserSetLoad")
```
Lädt die Werkseinstellungen. Das ist wichtig für die Reproduzierbarkeit: Egal was vorher jemand an der Kamera verstellt hat, jede Aufnahme startet vom selben Zustand. peakcvbridge macht das genauso.

```python
nm.FindNode("PixelFormat").SetCurrentEntry("Mono8")
nm.FindNode("ExposureTime").SetValue(exposure_ms * 1000)
```
Mono8 bedeutet 8 Bit Graustufen, ein Byte pro Pixel. `ExposureTime` erwartet Mikrosekunden, deshalb `* 1000`. Liegt der Wert außerhalb des erlaubten Bereichs, wirft das SDK eine Exception und das Skript bricht ab.

```python
fps = nm.FindNode("AcquisitionFrameRate")
fps.SetValue(fps.Maximum())
```
Auch im Trigger-Modus begrenzt `AcquisitionFrameRate`, wie schnell die Kamera Bilder aufnehmen darf. Kommt ein Triggerpuls früher als erlaubt, wird er ignoriert. Deshalb wird das Limit auf das Maximum gesetzt. Das Maximum hängt von der Belichtungszeit ab, darum steht diese Zeile **nach** dem Setzen der Belichtung.

```python
nm.FindNode("TriggerMode").SetCurrentEntry("On")
nm.FindNode("TriggerSource").SetCurrentEntry("Line0")
nm.FindNode("TriggerActivation").SetCurrentEntry("RisingEdge")
```
Die Kamera nimmt jetzt nur noch dann ein Bild auf, wenn an Eingang Line0 eine steigende Flanke (Low → High) ankommt, also bei jedem Puls des Arduino.

```python
ds = device.DataStreams()[0].OpenDataStream()
payload = nm.FindNode("PayloadSize").Value()
for _ in range(max(NUM_BUFFERS, ds.NumBuffersAnnouncedMinRequired())):
    ds.QueueBuffer(ds.AllocAndAnnounceBuffer(payload))
```
Der *DataStream* ist der Kanal, über den die Bilddaten von der Kamera zum NUC kommen. Das SDK schreibt jedes Bild in einen vorher reservierten Speicherbereich, einen *Buffer*:
- `PayloadSize` ist die Größe eines Bildes in Bytes.
- `AllocAndAnnounceBuffer` reserviert einen Buffer und meldet ihn beim SDK an.
- `QueueBuffer` stellt ihn in die Warteschlange der freien Buffer.

Es sind 16 Buffer, also ein Ringpuffer: Das SDK füllt freie Buffer, das Programm holt volle ab und gibt sie danach zurück. Bei 30 Hz überbrücken 16 Buffer gut eine halbe Sekunde, falls das Programm kurz langsamer ist.

Rückgabe ist `device, nm, ds`. `device` wird in `record()` zwar nicht direkt benutzt, muss aber als Variable erhalten bleiben. Sonst könnte Python das Objekt aufräumen und die Kamera schließen.

### 4.4 `close_camera(nm, ds)`

```python
execute(nm, "AcquisitionStop")                       # Kamera stoppt
ds.StopAcquisition(ids_peak.AcquisitionStopMode_Default)  # Datenstrom stoppt
ds.Flush(ids_peak.DataStreamFlushMode_DiscardAll)    # Warteschlange leeren
for buf in ds.AnnouncedBuffers():
    ds.RevokeBuffer(buf)                             # Buffer abmelden
nm.FindNode("TLParamsLocked").SetValue(0)            # Parameter wieder änderbar
nm.FindNode("TriggerMode").SetCurrentEntry("Off")    # Kamera frei laufen lassen
```
Die Aufräumreihenfolge ist umgekehrt zum Start. Am Ende wird der Trigger ausgeschaltet, damit andere Programme (z.B. der Live-Stream) die Kamera im normalen Modus vorfinden.

### 4.5 `write_frames(jobs)`

```python
while True:
    job = jobs.get()
    if job is None:
        return
    path, frame = job
    cv2.imwrite(path, frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
```
Diese Funktion läuft in mehreren Threads gleichzeitig. `jobs` ist eine `queue.Queue`, also eine thread-sichere Warteschlange. `jobs.get()` blockiert, bis ein Auftrag da ist, und jeder Auftrag wird von genau einem Thread abgeholt. `None` ist das vereinbarte Stoppsignal (ein sogenannter *Sentinel*). PNG-Kompressionsstufe 1 ist schnell bei trotzdem verlustfreier Speicherung.

Warum mehrere Threads: PNG-Kodierung dauert pro Bild ungefähr so lange wie der Abstand zwischen zwei Triggern. In den alten Aufnahmen hat ein einzelner Schreib-Thread 30 Hz nicht geschafft, und pro Kamera gingen 56 bis 74 Bilder verloren (`dropped_disk` in der alten `meta.json`). OpenCV gibt während `imwrite` den Python-GIL frei, deshalb kodieren vier Threads wirklich parallel auf mehreren CPU-Kernen.

### 4.6 `record(out_dir, duration, exposure_ms)`

```python
os.makedirs(os.path.join(out_dir, "frames"))
```
Ohne `exist_ok=True` wirft das einen Fehler, wenn der Ordner schon existiert. Das ist ein bewusster Schutz: Eine Aufnahme mit bereits benutztem Namen bricht sofort ab, statt alte Daten zu überschreiben oder zu vermischen.

```python
jobs = queue.Queue()
writers = [threading.Thread(target=write_frames, args=(jobs,)) for _ in range(NUM_WRITERS)]
for t in writers:
    t.start()
```
Die Queue hat kein Größenlimit. Das ist wichtig: Die Aufnahmeschleife darf **nie** warten müssen (siehe 4.7). Wenn die Platte kurz langsamer ist, wächst die Queue im RAM, statt Bilder zu verwerfen.

```python
nm.FindNode("TLParamsLocked").SetValue(1)
ds.StartAcquisition()
execute(nm, "AcquisitionStart")
```
`TLParamsLocked = 1` sperrt Parameter, die die Bildgröße ändern würden. Das ist während der Aufnahme Pflicht, weil die Buffer eine feste Größe haben. Dann wird zuerst der Datenstrom auf dem NUC gestartet und danach die Kamera. Ab jetzt wartet die Kamera auf Triggerpulse.

### 4.7 Die Aufnahmeschleife

```python
rows = []
t_end = time.monotonic() + duration
try:
    while time.monotonic() < t_end:
```
Für die **Dauer** wird `time.monotonic()` benutzt. Diese Uhr läuft garantiert gleichmäßig vorwärts und wird nicht von NTP verstellt. Für den **Zeitstempel** der Bilder (unten) wird dagegen `time.time_ns()` benutzt, die echte Uhrzeit, weil nur diese zwischen den NUCs vergleichbar ist.

```python
        try:
            buf = ds.WaitForFinishedBuffer(500)
        except ids_peak.TimeoutException:
            continue
```
Wartet bis zu 500 ms auf das nächste fertige Bild. Kommt keins (z.B. weil der Trigger aus ist), gibt es eine Timeout-Exception. `continue` springt zurück zum Schleifenanfang, wo erneut geprüft wird, ob die Zeit abgelaufen ist. Ohne Timeout würde das Programm bei fehlendem Trigger ewig hängen.

```python
        t_host = time.time_ns()
```
Der Zeitstempel wird **sofort** genommen, bevor irgendetwas anderes passiert. Er enthält die Übertragungszeit von der Kamera zum NUC. Solange diese Verzögerung auf allen NUCs etwa gleich ist, stört sie nicht, und ein konstanter Unterschied wird in `sync_frames.py` herausgerechnet. Die Einheit ist Nanosekunden seit 1970, als ganze Zahl.

```python
        if not buf.IsIncomplete():
            img = ids_peak_ipl_extension.BufferToImage(buf)
            frame = img.get_numpy_1D().reshape(img.Height(), img.Width()).copy()
```
Unvollständige Bilder (Übertragungsfehler) werden übersprungen. `BufferToImage` wandelt den SDK-Buffer in ein Bildobjekt. `get_numpy_1D()` liefert die Pixel als eindimensionales numpy-Array (Breite × Höhe Bytes), und `reshape` macht daraus ein 2D-Bild.

`.copy()` ist zwingend: Das numpy-Array zeigt direkt auf den Speicher des SDK-Buffers. Der Buffer wird gleich zurückgegeben und vom nächsten Bild überschrieben. Ohne Kopie würden die Schreib-Threads später ein falsches Bild speichern.

```python
            name = f"frames/{len(rows):06d}.png"
            jobs.put((os.path.join(out_dir, name), frame))
            rows.append((name, t_host))
        ds.QueueBuffer(buf)
```
`{...:06d}` formatiert die Zahl sechsstellig mit führenden Nullen (`000042`). So sortieren sich die Dateien alphabetisch in der richtigen Reihenfolge. Das Bild geht als Auftrag in die Queue, das Speichern passiert in den Threads. Name und Zeitstempel kommen in die Liste `rows`. `QueueBuffer(buf)` gibt den Buffer an das SDK zurück, damit er wieder befüllt werden kann. Das passiert auch bei unvollständigen Bildern, sonst würden die Buffer mit der Zeit ausgehen.

```python
except KeyboardInterrupt:
    print("stopped early")
```
SIGINT (Strg+C oder `pkill -INT` aus `nucs.sh`) erzeugt in Python eine `KeyboardInterrupt`-Exception. Sie wird hier abgefangen, damit der Rest normal weiterläuft: Kamera schließen und alle bisherigen Bilder speichern.

### 4.8 Abschluss

```python
close_camera(nm, ds)
for _ in writers:
    jobs.put(None)
for t in writers:
    t.join()
```
Nach der Kamera werden die Schreib-Threads beendet. Jeder Thread braucht sein eigenes `None`. Die `None`s liegen **hinter** allen noch offenen Bildern in der Queue, also werden erst alle Bilder gespeichert. `join()` wartet, bis jeder Thread fertig ist.

```python
with open(os.path.join(out_dir, "frames.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["file", "host_time_ns"])
    w.writerows(rows)
```
Die CSV wird erst am Ende in einem Rutsch geschrieben. Da die Threads Bilder in beliebiger Reihenfolge fertigstellen, ist die Liste im Hauptthread die einfachste Art, die richtige Reihenfolge zu behalten. `newline=""` ist die von Python empfohlene Einstellung für das csv-Modul.

### 4.9 `main()`

```python
p.add_argument("out_dir")
p.add_argument("duration", type=float, help="seconds")
p.add_argument("exposure_ms", type=float)
...
ids_peak.Library.Initialize()
record(...)
ids_peak.Library.Close()
```
`argparse` liest die drei Positionsargumente und wandelt sie mit `type=float` in Zahlen um. Die IDS-Bibliothek muss vor jeder Nutzung initialisiert und am Ende geschlossen werden. `Close()` steht nach `record()`, damit alle Kamera-Objekte (lokale Variablen in `record`) zu dem Zeitpunkt schon freigegeben sind.

---

## 5. `sync_frames.py`

Läuft auf dem Laptop nach `./nucs.sh fetch`:

```
python3 sync_frames.py recordings/<name>
```

Ergebnis in `recordings/<name>/synced/`:
```
nucN/000000.png  nucNE/000000.png  ...  nucSW/000000.png   <- ein Zeitpunkt
groups.csv       index, time_ns, Originaldatei pro Kamera
```

### 5.1 Grundidee an einem Zahlenbeispiel

Bei 30 Hz liegen die Pulse 33,3 ms auseinander. Die Bilder eines Pulses kommen auf den sechs NUCs innerhalb weniger Millisekunden an, zum Beispiel:

```
Puls 1:  nucS 1000.0  nucN 1001.9  nucNW 997.0  ...   (ms)
Puls 2:  nucS 1033.3  nucN 1035.2  nucNW 1030.3 ...
```
Innerhalb eines Pulses liegen die Zeiten also wenige ms auseinander, zwischen zwei Pulsen sind es 33 ms. Alles, was näher als 30 % der Periode (10 ms) beieinander liegt, gehört zu einem Puls.

### 5.2 `load_camera(cam_dir)`

```python
rows = list(csv.DictReader(f))
times = np.array([int(r["host_time_ns"]) for r in rows], dtype=np.int64)
files = [cam_dir / r["file"] for r in rows]
```
`DictReader` liest jede CSV-Zeile als Dict mit den Spaltennamen als Schlüssel. Die Zeitstempel werden als `int64` gespeichert. Ein `float` hätte bei Werten um 1,79·10¹⁸ nur eine Genauigkeit von etwa 256 ns, und bei Differenzen könnten Rundungsfehler entstehen. `cam_dir / "frames/000001.png"` ist `pathlib`-Syntax zum Zusammensetzen von Pfaden.

### 5.3 `estimate_offset(t, t_ref, period)`

Schätzt, um wie viel die Uhr einer Kamera gegenüber einer Referenzkamera konstant versetzt ist. Das sind der Rest-Fehler von NTP plus Unterschiede in der Übertragungszeit.

```python
i = np.clip(np.searchsorted(t_ref, t), 1, len(t_ref) - 1)
left, right = t_ref[i - 1], t_ref[i]
```
`np.searchsorted(t_ref, t)` findet für jeden Zeitstempel in `t`, an welcher Stelle er in die sortierte Liste `t_ref` einsortiert werden müsste. Der Referenz-Zeitstempel direkt davor ist `t_ref[i-1]`, der direkt danach `t_ref[i]`. `np.clip` begrenzt `i` auf den gültigen Bereich, damit am Anfang und Ende kein Index außerhalb der Liste entsteht. Alles passiert vektorisiert für alle Bilder auf einmal, ohne Python-Schleife.

```python
nearest = np.where(np.abs(t - left) < np.abs(t - right), left, right)
d = t - nearest
return int(np.median(d[np.abs(d) < period / 2]))
```
`np.where(bedingung, a, b)` wählt elementweise a oder b. Hier ist das der jeweils nähere Referenz-Zeitstempel. `d` sind die Abstände dazu. Abstände über einer halben Periode kommen von fehlenden Bildern und werden verworfen. Der **Median** der restlichen Abstände ist der Versatz. Anders als der Mittelwert ist er robust gegen einzelne Ausreißer.

Grenze der Methode: Ist der echte Versatz größer als eine halbe Periode (bei 30 Hz 16,7 ms), wird die Kamera dem falschen Puls zugeordnet. Die NTP-Synchronisation der NUCs ist also Voraussetzung.

### 5.4 `sync(rec)`: Vorbereitung

```python
cam_dirs = sorted(d for d in rec.iterdir() if (d / "frames.csv").exists())
names = [d.name for d in cam_dirs]
times, files = zip(*(load_camera(d) for d in cam_dirs))
```
Alle Unterordner mit einer `frames.csv` sind Kameras. `zip(*liste_von_paaren)` teilt eine Liste von `(times, files)`-Paaren in zwei Listen auf: alle `times` und alle `files`.

```python
period = np.median([np.median(np.diff(t)) for t in times])
```
`np.diff(t)` berechnet die Abstände aufeinanderfolgender Zeitstempel. Deren Median ist pro Kamera die Triggerperiode, und fehlende Bilder (doppelter Abstand) fallen dabei nicht ins Gewicht. Der Median über alle Kameras ergibt die Periode. Die Frequenz muss also nicht angegeben werden, sie wird aus den Daten bestimmt.

```python
ref = int(np.argmax([len(t) for t in times]))
offsets = [estimate_offset(t, times[ref], period) for t in times]
times = [t - o for t, o in zip(times, offsets)]
```
Referenz ist die Kamera mit den meisten Bildern, weil sie die wenigsten Lücken hat. Ihr eigener Versatz ergibt sich automatisch zu 0. Danach werden alle Zeitstempel um ihren Versatz korrigiert.

Die Ausgabe `offset to nucNE [ms]: nucN +1.9, ...` ist die wichtigste Kontrolle. Werte von wenigen ms sind normal. Werte in der Nähe einer halben Periode deuten auf ein NTP-Problem hin.

### 5.5 `sync(rec)`: Gruppieren

```python
all_t = np.concatenate(times)
cam = np.concatenate([np.full(len(t), c) for c, t in enumerate(times)])
row = np.concatenate([np.arange(len(t)) for t in times])
```
Alle Zeitstempel aller Kameras kommen in ein langes Array `all_t`. Parallel dazu merkt sich `cam`, von welcher Kamera jeder Eintrag stammt (`np.full(n, c)` ist ein Array aus n-mal dem Wert c). `row` merkt sich die Zeilennummer in der jeweiligen `frames.csv`. Eintrag `k` bedeutet also: Kamera `cam[k]`, Bild `row[k]`, Zeit `all_t[k]`.

```python
groups = []
for k in np.argsort(all_t, kind="stable"):
    if not groups or all_t[k] - groups[-1]["t0"] > TOLERANCE * period:
        groups.append({"t0": int(all_t[k]), "rows": {}})
    groups[-1]["rows"].setdefault(int(cam[k]), int(row[k]))
```
`np.argsort` liefert die Indizes in zeitlich sortierter Reihenfolge. Die Schleife läuft also über alle Bilder aller Kameras chronologisch:
- Liegt ein Bild mehr als die Toleranz hinter dem **ersten** Bild der aktuellen Gruppe (`t0`), beginnt eine neue Gruppe. `groups[-1]` ist die letzte Gruppe in der Liste.
- `rows` ist ein Dict Kamera → Zeilennummer. `setdefault` trägt nur ein, wenn die Kamera noch nicht in der Gruppe ist. Sollte eine Kamera zwei Bilder im selben Puls haben, gewinnt das erste.
- `kind="stable"` sorgt dafür, dass bei exakt gleichen Zeitstempeln die Reihenfolge fest ist. Dasselbe Eingangsmaterial ergibt also immer dasselbe Ergebnis.

```python
complete = [g for g in groups if len(g["rows"]) == len(names)]
```
Behalten werden nur Zeitpunkte, an denen **alle** Kameras ein Bild haben. Für die Triangulation sind so alle Ansichten immer vollständig.

### 5.6 `sync(rec)`: Schreiben

```python
out = rec / "synced"
for n in names:
    (out / n).mkdir(parents=True)
```
`parents=True` legt auch `synced/` selbst an. Existiert der Ordner schon, gibt es einen Fehler. Das schützt vor versehentlichem Überschreiben. Zum Wiederholen `synced/` löschen.

```python
for i, g in enumerate(complete):
    srcs = [files[c][g["rows"][c]] for c in range(len(names))]
    for n, src in zip(names, srcs):
        shutil.copy2(src, out / n / f"{i:06d}.png")
    w.writerow([i, g["t0"]] + [src.name for src in srcs])
```
Die Gruppen werden fortlaufend von 0 an nummeriert. Für jede Kamera wird die Originaldatei über `files[kamera][zeile]` gefunden und unter der neuen Nummer kopiert. `copy2` übernimmt dabei auch die Zeitstempel der Datei.

`groups.csv` hält fest, welche Originaldatei jeder Kamera zu welcher neuen Nummer gehört. Damit ist jede Zuordnung nachvollziehbar. `time_ns` ist die (korrigierte) Zeit des Pulses und wird von `detect_2d.py` benutzt.

---

## 6. Reproduzierbarkeit

Was das Ergebnis einer Aufnahme bestimmt, und wo es festgelegt ist:

| Einfluss | wo festgelegt |
|---|---|
| Kameraeinstellungen | `UserSet Default` + Belichtung, bei jeder Aufnahme neu gesetzt (`record.py`) |
| Belichtungszeit | Argument von `nucs.sh record`, **nirgends gespeichert**, deshalb in den Aufnahmenamen schreiben (z.B. `s6_15_30` = Belichtung 15 ms, 30 s) |
| Triggerfrequenz | `set-frequency.sh` am Camserver, wird von `sync_frames.py` aus den Daten gemessen und ausgegeben |
| Zeitbasis | NTP-synchronisierte Uhren der NUCs; die Offset-Ausgabe von `sync_frames.py` zeigt, ob das passt |
| Ordnernamen | feste Zuordnung in `nucs.sh` (IP → Position) |
| Zuordnung der Bilder | deterministisch (stabile Sortierung, feste 30 % Toleranz), dieselben Daten ergeben immer dasselbe `synced/` |
| Nachvollziehbarkeit | `groups.csv` verknüpft jede neue Nummer mit den Originaldateien |

Ablauf einer vollständigen Aufnahme:

```bash
# am Camserver (eigenes Terminal): Trigger starten, Frequenz nicht mehr ändern
./set-frequency.sh /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0

# am Laptop
./nucs.sh deploy                      # nur nach Code-Änderungen nötig
./nucs.sh record s6_15_30 30 15
./nucs.sh fetch s6_15_30
python3 sync_frames.py recordings/s6_15_30
```

Kurze Kontrolle nach jeder Aufnahme: Jeder NUC sollte bei `done:` etwa Frequenz × Dauer Bilder melden (30 Hz × 30 s ≈ 900). `sync_frames.py` sollte die richtige Frequenz und Offsets von wenigen ms zeigen, und fast alle Zeitpunkte sollten vollständig sein.
