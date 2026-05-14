---
name: lunch-planning
description: Mittagessen planen, Wochenvorschläge erstellen und im System speichern.
allowed-tools: get_current_date get_recipes get_lunch_plan save_lunch_plan
---

## Mittagessen-Planung

### Wochentag-Regeln
- Dienstags kocht Mama. Plane für Dienstag **niemals** ein Rezept ein und speichere
  auch keinen Eintrag. Weise in der Vorschlagsliste explizit darauf hin:
  "Dienstag: Mama kocht 👩‍🍳"

### Wochenplanung
Wenn kein Mittagessen für morgen geplant ist, erstelle einen Vorschlag für die fehlenden
Tage der nächsten Woche (nur Tage ohne bestehenden Eintrag – keine bereits geplanten Tage
überschreiben).

Vorgehensweise:
1. Rufe get_current_date auf, um das heutige Datum zu kennen.
2. Rufe get_lunch_plan einmalig auf: startDate = heute minus 84 Tage, endDate = Ende der
   nächsten Woche (nächster Sonntag). Diese Antwort liefert gleichzeitig:
   - Die bestehenden Einträge der nächsten Woche (werden nicht überschrieben)
   - Den vollständigen Verlauf der letzten 12 Wochen für die Abwechslungsprüfung
3. Rufe get_recipes auf, um alle verfügbaren Rezepte zu laden.
4. Wähle Rezepte für die Tage aus, die ab heute + 7 Tage noch nicht gefüllt sind, 
   nach diesen Kriterien (Priorität absteigend):
   a. Abwechslung: Prüfe den Verlauf aus Schritt 2. Gerichte, die in den letzten 2 Wochen
      vorkamen, möglichst vermeiden. Gerichte aus den letzten 4 Wochen nur wenn nötig.
      Das Feld lastPlanDate in den Rezeptdaten dient als zusätzlicher Hinweis, aber der
      tatsächliche Verlauf aus Schritt 2 hat Vorrang.
   b. Wähle für Wochentags (Mo-Fr) eher einfache, schnelle Rezepte, für das Wochenende 
      auch aufwändigere Gerichte. Du kannst als Hinweis hierzu auch die letzten Wochen betrachten.
   c. Vielfalt: Mische verschiedene Kategorien (Hauptgericht, Suppe, etc.) über die Woche.
   d. Saisonalität: Bei sonst gleichwertigen Optionen bevorzuge saisonale Zutaten
      (Mitteleuropa). Dies ist ein optionaler Tiebreaker, kein Pflichtkriterium.
5. Präsentiere den Vorschlag als übersichtliche Liste (Datum + Rezeptname).
6. Warte auf Bestätigung des Nutzers, bevor du speicherst.
   Speichere NIEMALS ohne explizite Bestätigung ("Annehmen", "Ja", "OK", "Speichern").

### Rezeptreferenzen beim Speichern
Beim Aufruf von save_lunch_plan übergibst du im Feld recipes eine Liste von Objekten
mit mindestens {"id": <rezept-id>}. Das Tool lädt die vollständigen Rezeptdaten intern
selbst nach – übergib keine vollständigen Rezeptobjekte.

### Änderungswünsche
Wenn der Nutzer einzelne Tage ändern möchte, passe den Vorschlag entsprechend an und
zeige die aktualisierte Liste. Warte erneut auf Bestätigung.

### Bestätigung und Speichern
Erst nach expliziter Bestätigung rufst du save_lunch_plan für jeden Tag auf.
Gib anschließend eine Zusammenfassung aus, was gespeichert wurde.
