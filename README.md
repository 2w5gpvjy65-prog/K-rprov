# Körprov – tidigaste lediga tid

Söker igenom **alla provorter i Sverige** efter den tidigaste lediga
uppkörningstiden (förvalt: körprov B, **automat**) och visar en topplista.
Skriptet bokar ingenting, du bokar själv på https://fp.trafikverket.se/Boka/.

Trafikverkets bokning kräver BankID, så du måste logga in en gång.
Sessionen gäller ungefär en timme.

## Kom igång (på din egen dator)

```bash
pip install -r requirements.txt
playwright install chromium

python korprov.py login   # ett webbläsarfönster öppnas, logga in med BankID
python korprov.py sok     # söker alla orter, ca 2 minuter
```

Exempel på utskrift:

```
Tidigaste lediga tider (15 av 62 orter med tider):

  Datum      Tid    Provort                      Pris
  2026-11-15 13:30  Kiruna                       800 kr
  ...

TIDIGAST I SVERIGE: 2026-11-15 kl 13:30 i Kiruna
```

(Raderna ovan visar bara formatet, de är inga riktiga tider.)

## Alternativ: utan Playwright

Logga in på https://fp.trafikverket.se/Boka/ i din vanliga webbläsare, öppna
utvecklarverktygen (F12) → Nätverk, klicka på valfri request till
`fp.trafikverket.se` och kopiera värdet på `Cookie`-headern. Sedan:

```bash
export KORPROV_COOKIE='...klistra in här...'
python korprov.py sok
```

Cookien ger åtkomst till ditt Trafikverket-konto tills den går ut, så dela
den inte med någon.

## Inställningar

```
python korprov.py sok --fordon Manuell        # manuell växellåda i stället
python korprov.py sok --topp 30               # visa fler orter
python korprov.py sok --fordrojning 3         # långsammare (snällare mot servern)
```

## Bra att veta

- API:et är odokumenterat och kan ändras utan förvarning. Om provtyp eller
  fordonstyp inte hittas skriver skriptet ut vilka namn som finns.
- Fel 400/401/403 eller timeout betyder oftast att sessionen gått ut, så kör
  `login` igen.
- Skriptet väntar ca 1,5 s mellan anropen för att inte belasta Trafikverket.
