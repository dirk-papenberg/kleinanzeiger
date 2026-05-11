---
name: lunch-planning
description: Mittagessen planen, Wochenvorschläge erstellen, Regeln lernen und im System speichern.
allowed-tools: get_current_date get_skill_addon update_skill_addon get_recipes get_lunch_plan save_lunch_plan
---

## Mittagessen-Planung

Rufe bei jeder Mittagessen-Aufgabe zuerst get_skill_addon mit
skill_name="lunch-planning" auf und befolge die dort geladenen Regeln. Diese
Regeln liegen außerhalb des Container-Images und können dauerhaft angepasst
werden.

Wenn der Nutzer eine dauerhafte Planungsregel oder Präferenz formuliert, speichere
sie mit update_skill_addon für skill_name="lunch-planning" und bestätige kurz
die Aktualisierung.

Nutze get_current_date für Datumsbezug, get_lunch_plan/get_recipes für Vorschläge
und save_lunch_plan erst nach ausdrücklicher Bestätigung des Nutzers.
