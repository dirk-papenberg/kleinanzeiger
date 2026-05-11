---
name: lunch-planning
description: Mittagessen planen, Wochenvorschläge erstellen, Regeln lernen und im System speichern.
allowed-tools: get_current_date get_lunch_planning_skill update_lunch_planning_skill get_recipes get_lunch_plan save_lunch_plan
---

## Mittagessen-Planung

Rufe bei jeder Mittagessen-Aufgabe zuerst get_lunch_planning_skill auf und befolge
die dort geladenen Regeln. Diese Regeln liegen ausserhalb des Container-Images und
können dauerhaft angepasst werden.

Wenn der Nutzer eine dauerhafte Planungsregel oder Präferenz formuliert, speichere
sie mit update_lunch_planning_skill und bestätige kurz die Aktualisierung.

Nutze get_current_date für Datumsbezug, get_lunch_plan/get_recipes für Vorschläge
und save_lunch_plan erst nach ausdrücklicher Bestätigung des Nutzers.
