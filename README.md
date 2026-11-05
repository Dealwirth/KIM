# KIM – KI-Manager für Home Assistant

KIM ist eine Home-Assistant-Integration, die einen ganzen Stab von OpenHands-KI-Agenten
parallel an einer Aufgabe arbeiten lässt. Du gibst einfach ein Ziel ein – KIM zerlegt es
in Teilaufgaben, verteilt sie an die passenden Agenten, überwacht sie per Heartbeat und
übernimmt bei Ausfällen automatisch das Failover. Alle Agenten teilen sich einen
Notizzettel, über den sie sich abstimmen, ohne sich gegenseitig zu stören.

```
Ziel eingeben  ──►  Planner (DAG)  ──►  Zuweisung nach Rolle  ──►  Agenten arbeiten parallel
                                             │                          │
                                             ▼                          ▼
                                    Heartbeat + Failover  ◄────  gemeinsamer Notizzettel
```

## Funktionen

- **Ziel → Aufgabenplan (DAG).** Ein Ziel wird automatisch in eine Pipeline
  `research → implement → test → review` zerlegt, inklusive Abhängigkeiten.
  Alternativ kann ein fertiger JSON-Plan übergeben werden.
- **Parallele Agenten.** Mehrere Agenten arbeiten gleichzeitig; die maximale
  Parallelität wird global begrenzt, damit sie sich nicht in die Quere kommen.
- **Rollen und Funktion.** Jede Aufgabe hat Tags (z. B. `code`, `test`). Jeder Agent
  hat eine Rolle mit passenden Tags; die Zuweisung wählt den Agenten mit der besten
  Übereinstimmung.
- **Gemeinsamer Notizzettel.** Ein append-only JSONL-Log mit Dateisperre, in das alle
  Agenten Notizen, Ansprüche, Ergebnisse und Logs schreiben. So tauschen sie sich aus,
  ohne sich zu überschreiben.
- **Heartbeat & Ausfall-Management.** Jeder Agent meldet Lebenszeichen. Bleibt eines
  aus, wird die Aufgabe neu eingeplant oder nach zu vielen Versuchen endgültig als
  fehlgeschlagen markiert. Die Agenten verwalten sich damit gegenseitig.
- **Steuerung per Sprache/Assist.** Über den Conversation-Agenten steuerst du KIM im
  Chat. Services stehen zusätzlich für Automationen bereit.

## Installation

1. Ordner `custom_components/kim` nach `<HA-Konfiguration>/custom_components/kim`
   kopieren (bei HACS: als Custom Repository hinzufügen).
2. Home Assistant neu starten.
3. Einstellungen → Geräte & Dienste → Integration hinzufügen → **KIM KI-Manager**.
4. Backend wählen:
   - `mock` – komplett offline, ideal zum Ausprobieren.
   - `local` – ein lokal laufender OpenHands-Agent-Server.
   - `cloud` – OpenHands Cloud (API-Schlüssel erforderlich).

## Bedienung

### Über Assist sprechen

| Eingabe | Wirkung |
| --- | --- |
| `Baue eine Login-Seite` | Neues Ziel, KIM plant und verteilt |
| `plan: Kontaktformular bauen` | Wie oben, aber explizit als Plan |
| `zustand` | Kurzer Statusbericht |
| `pausiere` / `weiter` | Zuweisung stoppen / fortsetzen |
| `notiz: Bitte Tests erweitern` | Notiz auf den gemeinsamen Notizzettel |

### Services (für Automationen)

| Service | Felder | Zweck |
| --- | --- | --- |
| `kim.submit` | `goal`, optional `title`, `plan` | Ziel einreichen |
| `kim.cancel` | `task_id` | Aufgabe abbrechen |
| `kim.retry` | `task_id` | Aufgabe erneut versuchen |
| `kim.pause` / `kim.resume` | – | Zuweisung pausieren / fortsetzen |
| `kim.note` | `text`, `author`, `task_id` | Notiz schreiben |

Beispiel:

```yaml
service: kim.submit
data:
  goal: "Baue eine REST-API für Bestellungen"
```

### Sensoren

| Sensor | Aussage |
| --- | --- |
| `sensor.kim_ki_manager_status` | idle / working / scheduling / paused + Zähler |
| `sensor.kim_ki_manager_agenten` | Anzahl Agenten, Details je Agent als Attribut |
| `sensor.kim_ki_manager_aufgaben` | erledigte Aufgaben, alle Pläne als Attribut |
| `sensor.kim_ki_manager_notizzettel` | Notizzettel-Einträge |

### Events

`kim_task_done`, `kim_task_failed`, `kim_worker_offline` – damit kannst du Automationen
auf den Fortschritt reagieren lassen.

## Wie die Agenten sich abstimmen

Der Notizzettel (`Scratchpad`) ist eine append-only JSONL-Datei. Schreibvorgänge werden
mit einer Dateisperre geschützt, sodass mehrere Agenten gleichzeitig schreiben können,
ohne sich zu überschreiben. Jede Zeile ist eine Nachricht:

- `note` – freie Information
- `claim` – ein Agent beansprucht eine Aufgabe (mit Ablaufzeit, damit veraltete
  Ansprüche verfallen)
- `result` – Ergebnis einer Aufgabe
- `heartbeat` – Lebenszeichen (nur im Speicher, damit die Datei nicht wächst)
- `log` – Fortschrittszeile

Dateizugriffe laufen in einem eigenen Hintergrund-Thread. Der Home-Assistant-Event-Loop
wird dadurch nie blockiert.

## Entwicklung und Tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install pytest-homeassistant-custom-component
python -m pytest -q
```

Die Engine (`manager`, `planner`, `roles`, `scratchpad`, `models`) ist bewusst frei von
Home-Assistant-Abhängigkeiten und damit direkt testbar.

## Berechtigungen

- **OpenHands Cloud:** ein API-Schlüssel (Bearer-Token) für die App-Server-API.
- **Lokaler Agent-Server:** die Basis-URL und – falls gesetzt – der Session-API-Key.
- **Home Assistant:** Netzwerkzugriff aus der Integration sowie Schreibzugriff auf
  `<HA-Konfiguration>/.storage/` für den Notizzettel.

## Verbesserungsvorschläge

- **LLM-Planer statt Heuristik.** Der Heuristik-Plan ist deterministisch; ein
  Planer-Agent (oder ein LLM-Aufruf) könnte den DAG feiner und aufgabenspezifisch
  schneiden. Der Code nimmt bereits einen fertigen JSON-Plan entgegen (`kim.submit`
  mit `plan`), das ist der natürliche Anschlusspunkt.
- **Persistente Pläne.** Aktuell liegen Pläne im Speicher. Ein Neustart verliert sie.
  Ein HA-`Store` würde sie über Neustarts hinweg erhalten.
- **Kosten-/Zeitlimits pro Aufgabe.** Ein Budget pro Aufgabe verhindert, dass ein
  Agent endlos arbeitet.
- **Retry-Strategie mit Backoff.** Statt sofortiger Neuverteilung ein wachsender
  Abstand zwischen den Versuchen.
- **Feinere Rollen-Auswahl.** Skills und ein Lastmaß je Agent statt reiner Tag-Schnittmenge.
