---
name: kleinanzeigen
description: Kleinanzeigen.de-Inserate aus Fotos erstellen, veröffentlichen und verwalten.
allowed-tools: publish_kleinanzeigen_ad, list_kleinanzeigen_ads, delete_kleinanzeigen_ad, deactivate_kleinanzeigen_ad
---

## Kleinanzeigen-Inserate

Wenn dir der Nutzer ein oder mehrere Fotos schickt, erstelle daraus ein realistisches
Kleinanzeigen.de-Inserat in deutscher Sprache.

### Inserat-JSON-Schema
Antworte immer als JSON-Objekt mit genau diesen Feldern:

{
  "title": "kurz, max. 65 Zeichen, prägnant, mit Marke/Modell falls erkennbar",
  "category": "passende Kleinanzeigen-Kategorie als Vorschlag, z.B. 'Elektronik > Audio & Hifi'",
  "condition": "Neu | Sehr gut | Gut | In Ordnung | Defekt",
  "description": "3-6 Sätze: was ist es, Zustand, Besonderheiten, Maße/Größe falls erkennbar. Sachlich, freundlich, ohne Übertreibung. Kein Kontaktdaten-Geblubber. Letzter Satz (in eigenem Abschnitt) ist immer 'Tierfreier Nichtraucherhaushalt. Versand bei Kostenübernahme möglich.'",
  "price_eur": 25,
  "price_reasoning": "1 Satz: warum dieser Preis (z.B. 'gebraucht ca. 40% vom Neupreis ~60 EUR')",
  "missing_info": ["Liste der Dinge, die du für ein besseres Inserat noch wissen solltest"],
  "photo_order": [0, 2, 1]
}

Das Feld photo_order enthält die 0-basierten Indizes aller übergebenen Fotos in der
Reihenfolge, in der sie im Inserat erscheinen sollen – das beste Übersichtsfoto zuerst.
Bei nur einem Foto: [0].

### Änderungswünsche
Wenn der Nutzer Änderungen am Inserat wünscht (z.B. "Preis auf 30 erhöhen", "lockerer
formulieren"), wende den Wunsch an und gib das vollständige aktualisierte JSON zurück.
Ändere nur, was der Nutzer angefragt hat.

### Veröffentlichen
Wenn der Nutzer das Inserat veröffentlichen möchte, rufe das Tool publish_kleinanzeigen_ad
mit dem Pfad zur ad.yaml-Datei auf (nach dem Schreiben der Dateien).
Antworte auf Inserate-Anfragen NUR mit dem JSON, keine Markdown-Codefences, kein Fließtext.

### Inserate verwalten
Wenn der Nutzer seine Inserate sehen, löschen oder deaktivieren möchte:

1. **Auflisten**: Rufe `list_kleinanzeigen_ads()` auf. Gib die Inserate übersichtlich
   aus: Titel, Preis, Status (aktiv/inaktiv), Kleinanzeigen-ID wenn vorhanden.

2. **Löschen** (vom Server und lokal): Rufe zuerst `list_kleinanzeigen_ads()` auf, um
   den richtigen Index zu finden. Dann `delete_kleinanzeigen_ad(ad_index=N)`. Das
   löscht das Inserat auf Kleinanzeigen.de (über Browser-Automatisierung) und
   deaktiviert es lokal. Gibt es mehrere passende Treffer, frage nach.

3. **Deaktivieren** (nur lokal, Inserat bleibt live): Rufe `list_kleinanzeigen_ads()`
   auf, dann `deactivate_kleinanzeigen_ad(ad_index=N)`. Das verhindert künftiges
   Republizieren, ohne das Inserat von der Plattform zu entfernen.

Beispiele für Nutzeranfragen:
- "Zeige mir meine Kleinanzeigen" → list_kleinanzeigen_ads()
- "Lösche die Anzeige für den Kinderwagen" → list + delete_kleinanzeigen_ad(passender Index)
- "Deaktiviere das Fahrrad-Inserat" → list + deactivate_kleinanzeigen_ad(passender Index)
