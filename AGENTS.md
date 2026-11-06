# AGENTS.md – KIM (KI-Manager für Home Assistant)

## Projekt
Home-Assistant-Custom-Integration `kim`, die einen Pool von OpenHands-Agenten
orchestriert (Planung als DAG, Zuweisung nach Rollen, Heartbeat, Failover,
gemeinsamer Notizzettel).

## Struktur
- `custom_components/kim/` – die Integration. `manager.py`, `planner.py`, `roles.py`,
  `scratchpad.py`, `models.py` sind bewusst **frei von Home-Assistant-Imports** und
  werden direkt unit-getestet.
- `custom_components/kim/backends.py` – `CloudBackend` (OpenHands Cloud),
  `LocalBackend` (lokaler Agent-Server), `MockBackend` (offline/Tests).
- `custom_components/kim/runtime.py` – Brücke zu HA: Coordinator, Services, Lebenszyklus.
- `custom_components/kim/{sensor,conversation}.py` – HA-Entitäten.
- `tests/` – pytest; HA-Tests laufen über `pytest-homeassistant-custom-component`.

## Befehle
```bash
. .venv/bin/activate && python -m pytest -q
```
Das Test-venv ist bereits eingerichtet (Python 3.13, HA 2026.x).

## Wichtige Fallstricke
- `tests/conftest.py` schiebt das Repo-Root an den Anfang von `sys.path` und entfernt
  ein evtl. vorgeladenes `custom_components`-Paket. Ohne das gewinnt das HA-Test-
  Harness und `custom_components.kim` wird nicht gefunden.
- `custom_components/__init__.py` muss existieren (Namespace-Paket-Konflikt).
- `custom_components/kim/manifest.json` ist Pflicht, sonst lädt die Integration nie.
- Der Scratchpad schreibt über einen **Hintergrund-Thread** (kein blockierendes
  Datei-I/O im Event-Loop). Heartbeats sind transient (`persist=False`) und werden
  nicht auf Platte geschrieben. Tests müssen die Writer-Threads schließen – dafür
  sorgt die Fixture `_close_scratchpads` in `tests/conftest.py`, die von
  `verify_cleanup` abhängt, damit der Thread-Check des Harness nicht fehlschlägt.

## Docker-Smoke-Test (echtes Home Assistant)
```bash
docker run -d --name kim-ha --network host -v /tmp/ha-config:/config \
  ghcr.io/home-assistant/home-assistant:stable
```
- HA `stable` erwartet in `.storage/core.config_entries` `minor_version` 5 sowie pro
  Eintrag `discovery_keys` und `subentries`, sonst `KeyError: 'discovery_keys'`.
- `/tmp/ha-config` gehört root; vor dem Kopieren `sudo chown -R $(id -u):$(id -g) /tmp/ha-config`.
- Login für die REST-API: Onboarding-Flow (`/api/onboarding/users`) liefert einen
  `auth_code`, der gegen `/auth/token` getauscht wird. Der `password`-Grant-Typ
  funktioniert nicht.
- Live-Test eines Auftrags: `POST /api/services/kim/submit {"goal": "..."}`.

## Live-Betrieb (echte HA-Instanz petryss.duckdns.org:8123)
- Integration per HACS (Repo-ID `1446865836`) installieren/aktualisieren und HA neu
  starten. Danach meldet `sensor.kim_ki_manager_status` `worker_count`.
- Backend live umschalten: Options-Flow über
  `POST /api/config/config_entries/options/flow {"handler": "<entry_id>"}` starten und
  mit `POST .../options/flow/<flow_id>` das Backend setzen. Für Tests reicht
  `backend: mock` (kostet nichts, kein Cloud-Kontingent).
- **Cloud-Tageslimit:** OpenHands Cloud erlaubt standardmäßig 20 Sandboxes pro Tag.
  Ist das Limit erreicht, liefert der Start-Task `status: ERROR` mit
  „Daily sandbox limit of 20 reached". KIM setzt den Cloud-Backend dann für eine
  Stunde auf Cooldown und fällt auf den nächsten Backend zurück. **Wichtig:** Der
  `FailoverBackend.status()` liefert `degraded` + `reason`; das Panel zeigt damit
  eine deutliche Warnung. Ohne diese Warnung sähen Mock-Antworten wie echte
  Agenten-Ergebnisse aus (das war die Ursache für „funktioniert nicht").
- **Mock-Ergebnisse** sind mit `[SIMULATION]` gekennzeichnet, damit ein
  degradierter Lauf nie für echte Agentenarbeit gehalten wird.
- **Modell gesperrt:** Ist das Team nicht für ein Modell freigeschaltet, antwortet
  der Start mit „team not allowed to access model". `is_model_error()` erkennt das,
  der Manager stoppt das Spawnen (`_config_error`) statt endlos Sandboxes zu
  erzeugen. Im Config-Flow gibt es deshalb ein Modell-Dropdown (`KNOWN_MODELS`).
  Standard und einzig sicherer Wert: `openhands/deepseek-v4.1-flash`.
- **Auto-Aufräumen:** `Manager.cleanup_when_idle` (in `runtime.py` aktiv) beendet
  alle Agenten und löscht deren Konversationen, sobald keine Aufgabe mehr offen ist.
  So bleibt das Tageskontingent für den nächsten Lauf frei.
- **HA-Kontext:** `KimRuntime._home_context()` fasst echte Entitäten/Bereiche
  zusammen und wird über `context_provider` an jeden Aufgaben-Prompt gehängt -
  Agenten argumentieren mit echten Hausdaten statt zu raten.
- **Archiv laden:** `kim/archive/load` ruft `Manager.restore_archive()` auf und
  stellt Plan/Aufgaben wirklich wieder her (früher kam nur JSON zurück, das Panel
  zeigte nichts).
- **Artefakte/PDF-Ausgabe:** Der Cloud-`/download`-Endpunkt liefert **nur das
  Event-Transcript**, keine Workspace-Dateien. Echte Deliverables (PDFs, Reports)
  kommen vom Sandbox-Agent-Server über `GET /api/file/download?path=<datei>` bzw.
  `GET /api/file/archive?path=<ordner>&format=tar.gz` (Header `X-Session-API-Key`).
  Der Workspace-Pfad steht im Konversations-Tag `archiveworkspacepath`
  (meist `/workspace/project`) und wird beim Start in `Worker.workspace` gemerkt.
  `fetch_file`/`fetch_archive` nutzen diese Endpunkte; `fetch_events_archive`
  bleibt für Diagnose.
- **Nichts geht mehr verloren:** `Manager.before_teardown` (von `runtime.py`
  gesetzt) archiviert die Agent-Workspaces, **bevor** `cleanup_when_idle` die
  Sandbox löscht. `_retire_worker()` ruft den Hook in allen Teardown-Pfaden
  (idle, cleanup, stop) auf. Die Archive werden nach `config/www/kim/<task_id>/`
  entpackt und über `/local/kim/<task_id>/...` ausgeliefert; die Dateiliste steht
  im Snapshot unter `artifacts` und im Panel-Abschnitt „Ergebnisse".
- **Ausgabe-Ordner:** Agenten werden im Prompt angewiesen, Deliverables unter
  `OUTPUT_DIR` (`/workspace/project/output`) als echte Dateien abzulegen
  (PDFs wirklich erzeugen, z. B. pandoc/reportlab).
- Access-Token läuft ab: neu holen per `/auth/login_flow` → `/auth/token`. Der
  `authorization_code`-Grant braucht `Content-Type: application/x-www-form-urlencoded`
  (nicht JSON), sonst `unsupported_grant_type`.
- **Nicht löschen:** Die 21 Konversationen in `app-conversations/search` sind eigene
  Projekte des Nutzers (Durak, PV-Wärmepumpe …), nicht von KIM. Nur Einträge mit
  Titel-Präfix `KIM ` stammen von KIM.
