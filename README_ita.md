<p align="center">
  <img src="images/HYDRA_UMC_BANNER.svg" alt="HYDRA-UMC-DATALAKE banner" width="100%">
</p>

# 🗄️ HYDRA-UMC-DATALAKE

<p align="center"><a href="README.md">🇺🇸 English</a> | <a href="README_spa.md">🇪🇸 Español</a> | <a href="README_fra.md">🇫🇷 Français</a> | 🇮🇹 <b>Italiano</b> | <a href="README_deu.md">🇩🇪 Deutsch</a> | <a href="README_zho.md">🇨🇳 简体中文</a> | <a href="README_jpn.md">🇯🇵 日本語</a></p>

### 📊 Archiviazione scalabile di serie temporali per dati robotici industriali

<p align="left">
  <img src="https://img.shields.io/badge/Licenza-GPL%203.0-blue.svg" alt="GPL 3.0">
  <img src="https://img.shields.io/badge/Archiviazione-SQLite3-003B57.svg" alt="Storage">
  <img src="https://img.shields.io/badge/Analisi-Big%20Data%20Ready-blue.svg" alt="Analytics">
</p>

---

## 1. 🛠️ PANORAMICA TECNICA

**HYDRA-UMC-DATALAKE** è l'archivio attuale di serie temporali della fabbrica. Fornisce un repository reale basato su SQLite per la telemetria normalizzata generata dall'ecosistema, comprese correnti del motore, angoli dei giunti, letture dei sensori e log di inferenza AI.

È la base software per analisi, manutenzione predittiva e report di produzione. L'implementazione SQLite attuale è testata localmente; un deployment esterno InfluxDB/TimescaleDB resta una futura decisione infrastrutturale, non una funzionalità dichiarata già in esecuzione.

### Caratteristiche principali:
* 🗄️ **Archiviazione basata su SQLite:** Archiviazione di serie temporali reale, ACID e su disco con `sqlite3` della stdlib Python. *(implementato)*
* 📊 **Schema dati unificato:** Telemetria normalizzata in formato lungo (`source/kind/field/timestamp/value`) per fonti HYDRA-UMC e URTC. *(implementato)*
* 🔍 **Query deterministiche:** I risultati sono ordinati per timestamp e spareggi stabili; le letture limitate rifiutano limiti non positivi. *(implementato)*
* 🔁 **Gestione idempotente dei retry:** La nuova consegna di un punto `(source, kind, field, timestamp)` sostituisce il valore (ultima scrittura vince), evitando che i retry gonfino i duplicati. *(implementato)*
* 🧬 **Migrazioni di Schema Reversibili:** `migrate_up()`/`migrate_down()` reali e testate, tracciate tramite il `PRAGMA user_version` proprio di SQLite - mai modificare una migrazione già rilasciata, aggiungerne una nuova. *(implementato)*
* 🕐 **Timestamp UTC Espliciti:** `GET /stats/range` riporta i dati reali più vecchi/recenti sia come ms grezzi sia come stringhe ISO 8601 UTC esplicite. *(implementato)*
* 🗑️ **Retention Validata:** Finestre di retention per serie, opt-in (`GET`/`POST /retention`, `POST /retention/apply`) - una finestra non positiva viene rifiutata categoricamente. *(implementato)*

---

## 2. 🔄 ARCHITETTURA DEI DATI

```mermaid
flowchart LR
    NODES["HydraNode e URTC"] --> COLL["TELEMETRY-COLLECTOR"]
    COLL --> LAKE["HYDRA-UMC-DATALAKE"]
    LAKE --> ANALY["ANOMALY-DETECTOR (AI)"]
    LAKE --> REP["PRODUCTION-REPORTS"]
    LAKE --> DASH["Dashboard STUDIO / SUITE"]
```

---

## 3. 🧱 ARCHITETTURA E DECISIONI DI PROGETTAZIONE

* **Perché è il genitore di integrazione, non un pari, dei suoi 3 figli.** HYDRA-UMC-TELEMETRY-COLLECTOR, HYDRA-UMC-ANOMALY-DETECTOR e HYDRA-UMC-PRODUCTION-REPORTS leggono/scrivono tutti sullo STESSO archivio di serie temporali sottostante - possedere quell'archivio in un unico posto (questo repo) evita 3 decisioni di schema indipendenti e potenzialmente divergenti.
* **Perché sqlite3 oggi, non ancora InfluxDB/TimescaleDB.** Un database esterno resta una possibile scelta di deployment a lungo termine, ma gestirlo è vero lavoro di infrastruttura e non qualcosa da dichiarare o aggiungere senza richiesta. Il `TimeSeriesStore` di `src/hydra_umc_datalake/store.py` è oggi un archivio di serie temporali reale, ACID e interrogabile (`sqlite3` della stdlib Python), non un placeholder, e resta dietro la propria classe affinché un backend futuro possa sostituirlo senza riscrivere il contratto HTTP.
* **Perché un'unica tabella "lunga" e stretta (source/kind/field/timestamp/value), non una colonna per campo di telemetria.** Il `Sample.Fields` proprio di HYDRA-UMC-TELEMETRY-COLLECTOR è aperto (qualsiasi nome di campo, qualsiasi fonte può segnalarne di nuovi) - uno schema stretto li accetta tutti senza una migrazione, al costo reale di una riga per campo per campione invece di una riga per campione.
* **Perché `aggregate()` fa un vero bucketing SQL per tempo, non solo `query()` grezza.** Una dashboard o un report che chiede "temperatura media del motore al minuto nell'ultima settimana" su milioni di righe grezze ha bisogno di un vero downsampling fatto dal database, non recuperato grezzo e mediato nel codice applicativo - i confini dei bucket di `aggregate()` sono deterministici (allineati al `start` della query stessa), quindi la stessa query sugli stessi dati traccia sempre gli stessi confini di bucket.
* **Come si inserisce nel resto dell'ecosistema.** Il genitore di integrazione della famiglia Data & Analytics - HYDRA-UMC-TELEMETRY-COLLECTOR lo alimenta da HYDRA-UMC-SERVER, HYDRA-UMC-ANOMALY-DETECTOR e HYDRA-UMC-PRODUCTION-REPORTS rileggono entrambi dalla sua stessa telemetria memorizzata.
* **Perché il versionamento dello schema usa il `PRAGMA user_version` proprio di SQLite, non una tabella fatta a mano.** SQLite fornisce già esattamente questo meccanismo reale (un intero nell'header del file) - una tabella di contabilità parallela sarebbe solo una seconda fonte di verità, potenzialmente divergente, per lo stesso fatto.
* **Perché la retention è opt-in per `(kind, field)`, non un default globale.** Un archivio con decine di serie di telemetria reali non dovrebbe avere l'assunzione di retention di un operatore applicata silenziosamente a ogni serie - `apply_retention()` tocca sempre e solo una serie a cui è stata esplicitamente assegnata una policy tramite `set_retention_policy()`/`POST /retention`.
* **Perché l'identità di retry è `(source, kind, field, timestamp)`.** Il contratto di telemetria normalizzato non possiede un ID di sequenza/evento; un punto esattamente ripetuto viene quindi trattato come un retry di rete incerto e consolidato con il comportamento deterministico dell'ultima scrittura. Ciò evita che righe duplicate distorcano conteggi e aggregati senza eseguire una pulizia distruttiva globale dei dati storici.
* **Perché `/stats/range` è un nuovo endpoint invece di estendere `/stats`.** La forma esistente di `/stats`, `{"sampleCount": <int>}`, è già reale e testata - aggiungervi campi sarebbe un cambiamento reale e distruttivo senza motivo, quando un secondo endpoint additivo non costa nulla.

---

## 📂 STRUTTURA DELLE CARTELLE

Servizio puramente software (integratore di ingestione/analisi) - senza hardware, firmware o sistema operativo propri; tali cartelle sono omesse secondo la politica della struttura del repository.

```text
HYDRA-UMC-DATALAKE/
├── src/hydra_umc_datalake/  # Codice sorgente
│   ├── __init__.py          # Versione del pacchetto
│   ├── store.py             # TimeSeriesStore: ingestione/query/aggregazione reali via sqlite3
│   ├── api.py                # Handler JSON/HTTP semplici che avvolgono lo store
│   └── main.py               # Punto di ingresso: collega store+API, avvia il server HTTP
├── tests/                   # pytest - logica dello store, migrazioni reali, round-trip HTTP reali
├── docs/
│   └── API.md               # Riferimento reale degli endpoint HTTP (richieste, risposte, codici di stato)
├── build/                   # Output di build (ignorato da git)
├── pyproject.toml           # Metadati del pacchetto, versione, dipendenze
├── bump_version.py          # Incremento di versione stile contachilometri (eseguito dal build)
├── docker-compose.yml       # Integra TELEMETRY-COLLECTOR / ANOMALY-DETECTOR / PRODUCTION-REPORTS
├── build.sh / build.bat     # Build reale: venv + installazione editable + bump + test
├── run.sh / run.bat         # Esecuzione reale: avvia l'API HTTP
└── README.md
```

Rimossi dal template originale: `hardware/`, `firmware/`, `os/`,
`images/` e `scripts/` — è un servizio puramente software (pacchetto
Python) senza hardware o firmware propri, senza un'immagine del sistema
operativo da mantenere, e senza contenuto di media/script di utilità
ancora sufficiente da giustificare cartelle proprie. Vedi
[`docs/API.md`](docs/API.md) per il riferimento completo degli endpoint HTTP.

---

## 4. ⚙️ BUILD ED ESECUZIONE

Richiede Python >= 3.10. Un vero archivio di serie temporali interrogabile
con API HTTP, non solo uno scheletro che si importa.

```bash
# Linux/macOS
./build.sh
./run.sh --port 8095

# Windows
build.bat
run.bat --port 8095
```

`build` crea/attiva un `.venv` locale, installa il pacchetto (editable, con
gli extra di dev) al suo interno, verifica l'import, ed esegue la vera
suite di test (`pytest`). `run` avvia l'API HTTP e inoltra qualsiasi flag
(`--addr`, `--port`, `--db`).

```bash
# Ingerire un campione (stessa forma normalizzata del Sample di HYDRA-UMC-TELEMETRY-COLLECTOR)
curl -X POST localhost:8095/ingest \
  -d '{"sourceId":"robot-1","kind":"motor_temp","timestamp":1700000000000,"fields":{"value":42.5}}'

# Interrogarlo di nuovo
curl "localhost:8095/query?sourceId=robot-1"

# Sottocampionare in bucket da 1 minuto su un intervallo di tempo reale
curl "localhost:8095/aggregate?kind=motor_temp&field=value&bucketMs=60000&start=0&end=1800000000000&agg=avg"

curl localhost:8095/stats
```

```bash
python -m pytest tests/ -v   # store.py (insert/query/aggregate, incluso
                              # calcolo di bucketing verificabile a mano)
                              # e api.py (round-trip HTTP reali contro un
                              # vero ThreadingHTTPServer su una porta
                              # effimera)
```

Per avviare questo progetto insieme ai suoi tre figli (Telemetry-Collector, Anomaly-Detector, Production-Reports) come directory gemelle:

```bash
docker compose up --build
```

---

## 🚀 ROADMAP
* **Fase 1:** Ingestione ad alto throughput del Datalake e indicizzazione per l'analisi storica.
* **Fase 2:** Compressione edge del collettore di telemetria e protocolli di trasmissione sicuri.
* **Fase 3:** Rilevamento delle anomalie tramite apprendimento non supervisionato e analisi delle vibrazioni del motore.
* **Fase 4:** Integrazione con Grafana per la visualizzazione avanzata in tempo reale e l'automazione dei report di produzione.

---

## 🔗 Progetti Correlati

Questo progetto fa parte di un ecosistema robotico più ampio dello stesso autore (JuanenRac / Electro Hobby 3D), che copre firmware, software di controllo, nodi IA e strumenti di flotta. Utile saperlo, perché una richiesta potrebbe in realtà riguardare uno di questi progetti anziché questo repository.

### Famiglia

**Genitore:** nessuno — questo progetto è esso stesso il genitore di integrazione della famiglia Data & Analytics.

**Figli:**
- **[HYDRA-UMC-TELEMETRY-COLLECTOR](https://github.com/JuanenRac/HYDRA-UMC-TELEMETRY-COLLECTOR)** — alimenta questo data lake con telemetria aggregata per robot.
- **[HYDRA-UMC-ANOMALY-DETECTOR](https://github.com/JuanenRac/HYDRA-UMC-ANOMALY-DETECTOR)** — esegue il rilevamento anomalie sulla telemetria memorizzata in questo data lake.
- **[HYDRA-UMC-PRODUCTION-REPORTS](https://github.com/JuanenRac/HYDRA-UMC-PRODUCTION-REPORTS)** — genera report di turno/OEE dalla telemetria memorizzata in questo data lake.

### Relazione Diretta (fuori dalla famiglia)

- **[HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER)** — la fonte dei log/telemetria ingeriti da questo progetto.

### Resto dell'Ecosistema

**Piattaforma HYDRA-UMC** — la cella di micro-fabbrica multi-robot
- **[HYDRA-UMC](https://github.com/JuanenRac/HYDRA-UMC)** — la scheda madre CM5 + STM32H745 che orchestra fino a 8 bracci robotici.
- **[HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER)** — il backend Express/WebSocket con cui parla ogni client di controllo.
- **[HYDRA-UMC-STUDIO](https://github.com/JuanenRac/HYDRA-UMC-STUDIO)** — dashboard di controllo web, visualizzazione 3D multi-robot.
- **[HYDRA-UMC-ANDROID-CONTROL](https://github.com/JuanenRac/HYDRA-UMC-ANDROID-CONTROL)** — app di controllo Android via Wi-Fi/Bluetooth.
- **[HYDRA-UMC-IOS-CONTROL](https://github.com/JuanenRac/HYDRA-UMC-IOS-CONTROL)** — app di controllo iOS/iPadOS costruita in Flutter.
- **[HYDRA-UMC-SUITE](https://github.com/JuanenRac/HYDRA-UMC-SUITE)** — centro di comando sciame desktop (Python/PySide6).
- **[HYDRA-UMC-EDITOR-URDF](https://github.com/JuanenRac/HYDRA-UMC-EDITOR-URDF)** — editor desktop di modelli URDF per il catalogo robot.
- **[HYDRA-UMC-DSI](https://github.com/JuanenRac/HYDRA-UMC-DSI)** — interfaccia touch nativa per lo schermo DSI a bordo.

**Piattaforma URTC** — il controller della testa utensile che ogni braccio HYDRA-UMC porta con sé
- **[URTC](https://github.com/JuanenRac/URTC)** — controller testa utensile su bus CAN, 25 profili utensile.
- **[URTC-FLASHER](https://github.com/JuanenRac/URTC-FLASHER)** — strumento desktop di flashing CAN-OTA + SWD/JTAG.
- **[URTC-TESTER](https://github.com/JuanenRac/URTC-TESTER)** — strumento desktop di diagnostica CAN live.
- **[URTC-WEB-STUDIO](https://github.com/JuanenRac/URTC-WEB-STUDIO)** — alternativa basata su browser via Web Serial API.

**🎥 Vision AI Node (Hailo-8)**
- [HYDRA-UMC-VISION-NODE](https://github.com/JuanenRac/HYDRA-UMC-VISION-NODE)
- [HYDRA-UMC-VISION-STREAMER](https://github.com/JuanenRac/HYDRA-UMC-VISION-STREAMER)
- [HYDRA-UMC-DETECTION-HEF](https://github.com/JuanenRac/HYDRA-UMC-DETECTION-HEF)
- [HYDRA-UMC-SAFETY-ZONES](https://github.com/JuanenRac/HYDRA-UMC-SAFETY-ZONES)
- [HYDRA-UMC-VISUAL-SERVOING-API](https://github.com/JuanenRac/HYDRA-UMC-VISUAL-SERVOING-API)

**🧠 Cognitive AI Node (Hailo-10)**
- [HYDRA-UMC-COGNITIVE-NODE](https://github.com/JuanenRac/HYDRA-UMC-COGNITIVE-NODE)
- [HYDRA-UMC-VLA-ENGINE](https://github.com/JuanenRac/HYDRA-UMC-VLA-ENGINE)
- [HYDRA-UMC-VOICE-UI](https://github.com/JuanenRac/HYDRA-UMC-VOICE-UI)
- [HYDRA-UMC-SEMANTIC-PLANNER](https://github.com/JuanenRac/HYDRA-UMC-SEMANTIC-PLANNER)
- [HYDRA-UMC-DOCS-QA](https://github.com/JuanenRac/HYDRA-UMC-DOCS-QA)

**🐝 Orchestration & Swarm**
- [HYDRA-UMC-ORCHESTRATOR](https://github.com/JuanenRac/HYDRA-UMC-ORCHESTRATOR)
- [HYDRA-UMC-SWARM-SYNC](https://github.com/JuanenRac/HYDRA-UMC-SWARM-SYNC)
- [HYDRA-UMC-PATH-PLANNER-3D](https://github.com/JuanenRac/HYDRA-UMC-PATH-PLANNER-3D)
- [HYDRA-UMC-JOB-DISPATCHER](https://github.com/JuanenRac/HYDRA-UMC-JOB-DISPATCHER)
- [HYDRA-UMC-NODE-HEALING](https://github.com/JuanenRac/HYDRA-UMC-NODE-HEALING)

**🎮 Digital Twin & Simulation**
- [HYDRA-UMC-TWIN](https://github.com/JuanenRac/HYDRA-UMC-TWIN)
- [HYDRA-UMC-PHYSICS-REPLICA](https://github.com/JuanenRac/HYDRA-UMC-PHYSICS-REPLICA)
- [HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE)
- [HYDRA-UMC-SYNTHETIC-DATA-GEN](https://github.com/JuanenRac/HYDRA-UMC-SYNTHETIC-DATA-GEN)

**🏭 Industrial Gateway**
- [HYDRA-UMC-GATEWAY-INDUSTRIAL](https://github.com/JuanenRac/HYDRA-UMC-GATEWAY-INDUSTRIAL)
- [HYDRA-UMC-OPCUA-SERVER](https://github.com/JuanenRac/HYDRA-UMC-OPCUA-SERVER)
- [HYDRA-UMC-MQTT-BROKER](https://github.com/JuanenRac/HYDRA-UMC-MQTT-BROKER)
- [HYDRA-UMC-MTCONNECT-ADAPTER](https://github.com/JuanenRac/HYDRA-UMC-MTCONNECT-ADAPTER)

**🛠️ Complementary Tools**
- [URTC-SMART-RACK](https://github.com/JuanenRac/URTC-SMART-RACK)
- [URTC-VISION-TOOL](https://github.com/JuanenRac/URTC-VISION-TOOL)
- [HYDRA-UMC-WATCH](https://github.com/JuanenRac/HYDRA-UMC-WATCH)
- [HYDRA-UMC-TOOL-CLI](https://github.com/JuanenRac/HYDRA-UMC-TOOL-CLI)
- [HYDRA-UMC-DASHBOARD-AI](https://github.com/JuanenRac/HYDRA-UMC-DASHBOARD-AI)


## 👤 AUTORE
**JuanenRac** (Electro Hobby 3D)
📧 electrohobby3d@gmail.com

## 📜 LICENZA
GPL-3.0 - Vedere LICENSE per i dettagli.

## 🛠️ BUILD & RUN

Usa il controllo di compilazione senza versionamento prima di una compilazione di rilascio:

| Azione | Windows | Linux / macOS |
|---|---|---|
| Controllo di compilazione (senza modificare versione o CHANGELOG) | `build-test.bat` | `./build-test.sh` |
| Esecuzione / sviluppo (se disponibile) | `run*.bat` o `dev*.bat` | `./run*.sh` o `./dev*.sh` |

`build-test.bat` e `build-test.sh` compilano o convalidano lo stack del progetto senza incrementare `hydra-umc.project.json` né modificare `CHANGELOG.md`. Possono creare solo i normali output del compilatore. Gli script esistenti `build*.bat`, `build*.sh`, `run*` e `dev*` mantengono il comportamento specifico di versione o esecuzione; usali quando tale comportamento è necessario.
