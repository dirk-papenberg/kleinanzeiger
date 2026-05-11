---
name: kleinanzeigen
description: Kleinanzeigen.de-Inserate aus Fotos erstellen und veröffentlichen.
allowed-tools: get_skill_addon update_skill_addon publish_kleinanzeigen_ad
---

## Kleinanzeigen-Inserate

Rufe bei jeder Kleinanzeigen-Aufgabe zuerst get_skill_addon mit
skill_name="kleinanzeigen" auf und befolge die dort geladenen dauerhaften
Nutzerregeln.

Wenn der Nutzer eine dauerhafte Inseratsregel oder Präferenz formuliert,
speichere sie mit update_skill_addon für skill_name="kleinanzeigen" und
bestätige kurz die Aktualisierung.

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
  "price_type": "FP | VB | VHB | zu verschenken",
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
