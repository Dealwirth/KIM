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
  „Daily sandbox limit of 20 reached". KIM überspringt dann nur die betroffene Rolle
  und versucht es im nächsten Tick erneut – der Rest des Pools läuft weiter.
- Access-Token läuft ab: neu holen per `/auth/login_flow` → `/auth/token`. Der
  `authorization_code`-Grant braucht `Content-Type: application/x-www-form-urlencoded`
  (nicht JSON), sonst `unsupported_grant_type`.
