# AIWerk content-loss guard

Ez az őr azt akadályozza meg, hogy egy upstream-szinkron merge csendben
eldobja a fork saját munkáját.

## Miért létezik

A `7b0cf741` Nous-szinkron merge 102 fájlt távolított el, amelyek az AIWerk
oldalon léteztek és az upstream szülőn soha. Külön törlő commit egyikhez sem
tartozott, és a merge zöld CI mellett ment át. A veszteség napokkal később,
használat közben derült ki.

## Mit néz

- **`protected_paths`** — nevesített fájlok, amelyeknek léteznie kell.
- **`markers`** — a fork védjegye (`aiwerk`) fájlonként. Ha egy fájlban a
  találatok száma **nullára** csökken, az blokkol; a puszta csökkenés jelent.
- **`selectors`** — tartalmi állítások (JSON-pointer) a védett fájlokon belül.
- **`protected_controls`** — maga az őr fájljai, hogy egy jelölt ne tudja
  kikapcsolni saját magát.

## Hogyan fut

`pull_request_target` triggerrel, tehát a **védett alapágból**, nem a jelölt
kódjából. A jelölt nem futtat kódot és nem kap titkot.

## Helyi futtatás

    python scripts/ci/content_loss_guard.py --help

A történeti önteszt a valódi `8bd56009 → 7b0cf741` merge-et játssza vissza, és
akkor is blokkolnia kell, ha a hozzá tartozó tesztek is eltűntek ugyanabban a
változtatásban.
