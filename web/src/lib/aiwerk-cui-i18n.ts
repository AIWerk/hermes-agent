// Hidden/customer-configured AIWerk Customer UI localization helpers.
// The user does not see a language switcher; the backend injects
// window.__AIWERK_CUI_LOCALE__ from tenant/profile config.

export type CuiLocale = "en" | "de" | "hu" | "fr" | "es" | "it" | "pt" | "ru" | "zh" | "zh-hant" | "ja" | "ko" | "tr" | "uk" | "af" | "ga";

const CUI_LOCALES: readonly CuiLocale[] = ["en", "de", "hu", "fr", "es", "it", "pt", "ru", "zh", "zh-hant", "ja", "ko", "tr", "uk", "af", "ga"];

export function readConfiguredCuiLocale(): CuiLocale {
  const raw = window.__AIWERK_CUI_LOCALE__?.trim().toLowerCase().replace("_", "-") ?? "";
  const aliases: Record<string, CuiLocale> = {
    "zh-cn": "zh",
    "zh-sg": "zh",
    "zh-tw": "zh-hant",
    "zh-hk": "zh-hant",
    magyar: "hu",
    hungarian: "hu",
    german: "de",
    deutsch: "de",
    english: "en",
  };
  const locale = aliases[raw] ?? raw.split(".", 1)[0];
  return (CUI_LOCALES as readonly string[]).includes(locale) ? (locale as CuiLocale) : "de";
}

export const SLASH_MENU_LABEL: Partial<Record<CuiLocale, string>> = {
  de: "Slash-Befehle",
  hu: "Slash parancsok",
};

const SLASH_CATEGORY_COPY: Record<string, Partial<Record<CuiLocale, string>>> = {
  Session: { de: "Sitzung", hu: "Munkamenet" },
  Configuration: { de: "Konfiguration", hu: "Beállítások" },
  "Tools & Skills": { de: "Tools & Skills", hu: "Eszközök és skillek" },
  Tools: { de: "Tools", hu: "Eszközök" },
  Info: { de: "Info", hu: "Információ" },
  TUI: { de: "TUI", hu: "TUI" },
  Skills: { de: "Skills", hu: "Skillek" },
};

const SLASH_COMMAND_COPY: Record<string, Partial<Record<CuiLocale, string>>> = {
  "/new": { de: "Neue Unterhaltung starten", hu: "Új beszélgetés indítása" },
  "/reset": { de: "Alias für /new", hu: "A /new aliasa" },
  "/fresh": { de: "Frische Sitzung mit den letzten Nachrichten als Lesekontext starten", hu: "Friss munkamenet indítása a legutóbbi üzenetek olvasási kontextusával" },
  "/clear": { de: "Bildschirm leeren und neue Sitzung starten", hu: "Képernyő törlése és új munkamenet indítása" },
  "/redraw": { de: "Oberfläche neu zeichnen", hu: "Felület újrarajzolása" },
  "/history": { de: "Unterhaltungsverlauf anzeigen", hu: "Beszélgetési előzmények megjelenítése" },
  "/save": { de: "Aktuelle Unterhaltung speichern", hu: "Aktuális beszélgetés mentése" },
  "/retry": { de: "Letzte Nachricht erneut senden", hu: "Utolsó üzenet újrapróbálása" },
  "/prompt": { de: "Nächsten Prompt im Editor schreiben und senden", hu: "Következő prompt megírása szerkesztőben és elküldése" },
  "/undo": { de: "Letzten User/Assistant-Austausch zurücknehmen", hu: "Utolsó felhasználó/asszisztens váltás visszavonása" },
  "/title": { de: "Sitzungstitel setzen", hu: "Munkamenet címének beállítása" },
  "/handoff": { de: "Sitzung an Telegram, Discord oder eine andere Plattform übergeben", hu: "Munkamenet átadása Telegramra, Discordra vagy más platformra" },
  "/branch": { de: "Aktuelle Sitzung verzweigen", hu: "Aktuális munkamenet elágaztatása" },
  "/fork": { de: "Alias für /branch", hu: "A /branch aliasa" },
  "/side": { de: "Temporäre Nebenunterhaltung starten", hu: "Ideiglenes mellékbeszélgetés indítása" },
  "/back": { de: "Zur Hauptsitzung zurückkehren", hu: "Visszatérés a fő munkamenethez" },
  "/compress": { de: "Kontext komprimieren", hu: "Kontextus tömörítése" },
  "/compact": { de: "Kompakte Anzeige umschalten", hu: "Kompakt nézet váltása" },
  "/rollback": { de: "Dateisystem-Checkpoints anzeigen oder wiederherstellen", hu: "Fájlrendszer checkpointok listázása vagy visszaállítása" },
  "/snapshot": { de: "Hermes Konfiguration und Zustand sichern oder wiederherstellen", hu: "Hermes konfiguráció és állapot mentése vagy visszaállítása" },
  "/stop": { de: "Alle laufenden Hintergrundprozesse stoppen", hu: "Minden futó háttérfolyamat leállítása" },
  "/background": { de: "Prompt im Hintergrund ausführen", hu: "Prompt futtatása háttérben" },
  "/bg": { de: "Alias für /background", hu: "A /background aliasa" },
  "/btw": { de: "Alias für /background", hu: "A /background aliasa" },
  "/agents": { de: "Aktive Agenten und laufende Aufgaben anzeigen", hu: "Aktív agentek és futó feladatok megjelenítése" },
  "/tasks": { de: "Alias für /agents", hu: "Az /agents aliasa" },
  "/queue": { de: "Prompt für den nächsten Turn vormerken", hu: "Prompt sorba állítása a következő körre" },
  "/q": { de: "Alias für /queue", hu: "A /queue aliasa" },
  "/steer": { de: "Laufende Antwort lenken, ohne sie zu unterbrechen", hu: "Futó válasz terelése megszakítás nélkül" },
  "/goal": { de: "Dauerziel setzen oder verwalten", hu: "Tartós cél beállítása vagy kezelése" },
  "/subgoal": { de: "Zusätzliche Kriterien zum aktiven Ziel hinzufügen", hu: "További kritériumok hozzáadása az aktív célhoz" },
  "/status": { de: "Sitzung, Modell, Tokens und Kontext anzeigen", hu: "Munkamenet, modell, tokenek és kontextus megjelenítése" },
  "/whoami": { de: "Eigenen Slash-Command-Zugriff anzeigen", hu: "Saját slash parancs jogosultság megjelenítése" },
  "/profile": { de: "Aktives Profil und Home-Verzeichnis anzeigen", hu: "Aktív profil és home könyvtár megjelenítése" },
  "/resume": { de: "Frühere Sitzung fortsetzen", hu: "Korábbi munkamenet folytatása" },
  "/sessions": { de: "Sitzungen durchsuchen und fortsetzen", hu: "Munkamenetek böngészése és folytatása" },
  "/config": { de: "Aktuelle Konfiguration anzeigen", hu: "Aktuális konfiguráció megjelenítése" },
  "/model": { de: "Modell wechseln", hu: "Modell váltása" },
  "/codex-runtime": { de: "Codex App-Server Runtime für OpenAI/Codex Modelle umschalten", hu: "Codex app-server runtime váltása OpenAI/Codex modellekhez" },
  "/personality": { de: "Vordefinierte Persönlichkeit setzen", hu: "Előre definiált személyiség beállítása" },
  "/statusbar": { de: "Kontext- und Modell-Statusleiste umschalten", hu: "Kontextus- és modell státuszsor váltása" },
  "/timestamps": { de: "Zeitstempel in Nachrichten und Verlauf umschalten", hu: "Időbélyegek váltása az üzenetekben és előzményekben" },
  "/verbose": { de: "Tool-Fortschrittsanzeige durchschalten", hu: "Eszközfolyamat-jelzés módjának váltása" },
  "/footer": { de: "Gateway-Footer ein- oder ausschalten", hu: "Gateway footer ki- vagy bekapcsolása" },
  "/yolo": { de: "YOLO-Modus umschalten", hu: "YOLO mód váltása" },
  "/reasoning": { de: "Reasoning-Aufwand und Anzeige verwalten", hu: "Gondolkodási szint és megjelenítés kezelése" },
  "/fast": { de: "Fast Mode umschalten", hu: "Gyors mód váltása" },
  "/skin": { de: "Display-Skin oder Theme anzeigen oder ändern", hu: "Megjelenítési skin vagy téma megjelenítése vagy módosítása" },
  "/language": { de: "UI-Sprache und passenden Standard-Skin anzeigen oder ändern", hu: "UI nyelv és kapcsolódó alapértelmezett skin megjelenítése vagy módosítása" },
  "/indicator": { de: "TUI Busy-Indikator auswählen", hu: "TUI foglaltságjelző kiválasztása" },
  "/voice": { de: "Voice Mode umschalten", hu: "Hang mód váltása" },
  "/busy": { de: "Festlegen, was Enter macht, während Hermes arbeitet", hu: "Annak beállítása, mit tegyen az Enter, miközben Hermes dolgozik" },
  "/tools": { de: "Tools verwalten", hu: "Eszközök kezelése" },
  "/toolsets": { de: "Verfügbare Toolsets anzeigen", hu: "Elérhető toolsetek megjelenítése" },
  "/skills": { de: "Skills suchen, installieren, ansehen oder verwalten", hu: "Skillek keresése, telepítése, megtekintése vagy kezelése" },
  "/memory": { de: "Ausstehende Speicherungen prüfen oder Freigabe-Gate umschalten", hu: "Függő memóriaírások ellenőrzése vagy jóváhagyási kapu váltása" },
  "/bundles": { de: "Skill-Bundles anzeigen", hu: "Skill csomagok megjelenítése" },
  "/pet": { de: "Petdex Maskottchen umschalten oder auswählen", hu: "Petdex kabala váltása vagy kiválasztása" },
  "/learn": { de: "Wiederverwendbaren Skill aus Beschreibung, URL, Ordner oder Chat lernen", hu: "Újrafelhasználható skill tanulása leírásból, URL-ből, mappából vagy chatből" },
  "/cron": { de: "Geplante Aufgaben verwalten", hu: "Ütemezett feladatok kezelése" },
  "/suggestions": { de: "Vorgeschlagene Automatisierungen prüfen", hu: "Javasolt automatizálások áttekintése" },
  "/blueprint": { de: "Automatisierung aus Blueprint-Vorlage einrichten", hu: "Automatizálás beállítása blueprint sablonból" },
  "/curator": { de: "Skill-Wartung im Hintergrund verwalten", hu: "Háttérben futó skill-karbantartás kezelése" },
  "/kanban": { de: "Multi-Profil Kollaborationsboard verwalten", hu: "Többprofilos együttműködési tábla kezelése" },
  "/reload": { de: ".env Variablen in die laufende Sitzung neu laden", hu: ".env változók újratöltése a futó munkamenetbe" },
  "/reload-mcp": { de: "MCP-Server neu laden", hu: "MCP szerverek újratöltése" },
  "/reload-skills": { de: "Skill-Slash-Befehle neu laden", hu: "Skill slash parancsok újratöltése" },
  "/browser": { de: "Browser-Tools per CDP mit dem lokalen Browser verbinden", hu: "Böngészőeszközök csatlakoztatása a helyi böngészőhöz CDP-n keresztül" },
  "/plugins": { de: "Installierte Plugins und Status anzeigen", hu: "Telepített pluginek és állapotuk megjelenítése" },
  "/mcp": { de: "MCP-Server verwalten", hu: "MCP szerverek kezelése" },
  "/help": { de: "Verfügbare Befehle anzeigen", hu: "Elérhető parancsok megjelenítése" },
  "/commands": { de: "Verfügbare Befehle anzeigen", hu: "Elérhető parancsok megjelenítése" },
  "/usage": { de: "Tokenverbrauch und Rate Limits anzeigen", hu: "Tokenhasználat és rate limit megjelenítése" },
  "/credits": { de: "Nous Guthaben anzeigen und aufladen", hu: "Nous kredit megjelenítése és feltöltése" },
  "/billing": { de: "Nous Terminal-Abrechnung verwalten", hu: "Nous terminál számlázás kezelése" },
  "/insights": { de: "Nutzungsanalysen anzeigen", hu: "Használati elemzések megjelenítése" },
  "/platforms": { de: "Gateway- und Messaging-Plattformstatus anzeigen", hu: "Gateway és üzenetküldő platform státusz megjelenítése" },
  "/copy": { de: "Letzte Assistant-Antwort in die Zwischenablage kopieren", hu: "Utolsó asszisztensválasz másolása a vágólapra" },
  "/paste": { de: "Bild aus der Zwischenablage anhängen", hu: "Kép csatolása a vágólapról" },
  "/image": { de: "Lokale Bilddatei an den nächsten Prompt anhängen", hu: "Helyi képfájl csatolása a következő prompthoz" },
  "/update": { de: "Hermes Agent aktualisieren", hu: "Hermes Agent frissítése" },
  "/version": { de: "Hermes Agent Version anzeigen", hu: "Hermes Agent verzió megjelenítése" },
  "/debug": { de: "Debug-Bericht hochladen und Links erzeugen", hu: "Debug jelentés feltöltése és linkek készítése" },
  "/quit": { de: "CLI beenden", hu: "CLI bezárása" },
  "/logs": { de: "Aktuelle Gateway-Logs anzeigen", hu: "Legutóbbi gateway logok megjelenítése" },
  "/mouse": { de: "Mouse Tracking Einstellung ändern", hu: "Egérkövetési beállítás módosítása" },
};

export function localizeSlashCommandDescription(command: string, description: string, locale: CuiLocale): string {
  if (locale === "en") return description;
  const key = command.toLowerCase();
  const direct = SLASH_COMMAND_COPY[key]?.[locale];
  if (direct) return direct;
  const aliasMatch = description.match(/^Alias for (\/\S+) — (.+)$/);
  if (aliasMatch) {
    const target = aliasMatch[1];
    if (locale === "de") return `Alias für ${target}`;
    if (locale === "hu") return `A ${target} aliasa`;
  }
  const skillMatch = description.match(/^Invoke the (.+) skill$/i);
  if (skillMatch) {
    if (locale === "de") return `${skillMatch[1]} Skill ausführen`;
  }
  return description;
}

export function localizeSlashCategory(category: string | undefined, locale: CuiLocale): string | undefined {
  if (!category || locale === "en") return category;
  return SLASH_CATEGORY_COPY[category]?.[locale] ?? category;
}
