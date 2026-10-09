# Plan 01 — Kern, Fintro en Firefly-inrichting

Status: **goedgekeurd; eerst uit te voeren, vóór [plan 02 (Argenta)](02-argenta.md).**
Alles hier is niet Argenta-specifiek, maar plan 02 bouwt erop. Voorbeelden zijn verzonnen
(publieke repo); echte rijen blijven in de terminal.

Harde eis bij elke stap: **een idle run blijft gratis** (AGENTS.md "Idle cost"). Alles hieronder
draait pas als er werk is; na stap 1 wordt de idle-meting herhaald, ≤ 2 ms.

## 1. Beslissingen

1. ✅ **Bestandsnamen in `data/` beschrijven de inhoud** (stap 1).
2. ✅ **`Valutadatum` → Firefly `interest_date`**, voor alle banken. De valutadatum is per
   definitie de datum vanaf/tot wanneer rente loopt; Firefly's `book_date` (boekdatum) is een
   ander begrip, en de boekdatum zit al in `date`. Het interne en normalized veld
   `booking_date` heet voortaan `interest_date` (stap 2).
3. ✅ **Vreemde munt** in de notities **én** in Firefly's velden `foreign_amount` +
   `foreign_currency_code`, voor alle banken, met een rekencontrole: een rij faalt als het niet
   op de cent klopt. Fintro: 29 rijen, 3 vaste vormen, bv. (verzonnen) `Bedrag: SEK -100,00
   Koers: 10,000000 Wisselkosten: 0,15 EUR` → 100 ÷ 10 + 0,15 = 10,15 = het EUR-bedrag. De koers staat niet
   altijd in dezelfde richting (USD vermenigvuldigen, SEK/GBP delen) → de controle probeert beide.
4. ✅ **Munten: geen lijst.** De transacties zeggen zelf welke munten nodig zijn; de importer
   schakelt een uitgeschakelde munt in (stap 3).
5. ✅ **Eigen rekeningen via een bestand** dat de feeder in Firefly toepast (stap 5), met
   gewone woorden voor de soort rekening.
6. ✅ **Dat bestand komt per SFTP van de desktop** op de server; ophalen uit Forgejo blijft een
   gedocumenteerde optie voor later (stap 5).
7. ✅ **Wat met de hand moet, staat in één checklist** in AGENTS.md (stap 6).
8. ✅ **Rabobank.be** (bankcode 844, bevestigd door een brief van de bank), afgesloten, geen
   exports meer. De Fintro- en Argenta-exports tonen drie Rabobank-IBAN's (nummers, bedragen
   en periodes alleen in de chat):

   | Rekening | Rol volgens de data |
   |---|---|
   | oude zichtrekening | één overschrijving naar Fintro, daarna niets meer |
   | spaarrekening | krijgt alleen geld, vanaf de Fintro-rekeningen |
   | zichtrekening | geeft alleen geld: naar Fintro, en bij het afsluiten naar Argenta |

   Spaar→zicht ging intern bij Rabobank en staat nergens. Wat er uitging min wat er inkwam is
   alles wat niet zichtbaar is, vermoedelijk interest.
   Gekozen: **één** Firefly-rekening voor spaar + zicht samen: spaar-IBAN in het IBAN-veld,
   zicht-IBAN in het veld "rekeningnummer". De importer herkent eigen rekeningen op beide
   velden (stap 3), dus geld naar de spaar- én van de zichtrekening komt op die ene rekening;
   het saldo = wat er samen bij Rabobank stond. Eén correctieboeking `Rabobank: rest,
   vermoedelijk interest` op de slotdatum zet het op 0. De oude zichtrekening: eigen afgesloten
   rekening, met als beginsaldo het bedrag van die ene overschrijving, eindigt op 0. De
   bedragen komen uit de data en gaan in het rekeningbestand, niet in de repo.
   Afgewezen: twee rekeningen met per uitbetaling een afgeleide overschrijving spaar→zicht.

9. ✅ **Documentatie: elk feit op één plek** (stap 0). Altijd geldend → AGENTS.md /
   architectural_patterns.md (elke sessie ingeladen); per bank → `engine/banks/<bank>/README.md`
   (alleen ingeladen als iemand aan die bank werkt); wat andere tools mogen gebruiken →
   `docs/output-contract.md`. Persoonlijk (welke rekeningen, classificatie, rapportering) staat
   in de privé-financerepo, die naar dit contract linkt; deze repo noemt die enkel als "een
   privétool".
10. ✅ **Unieke rijsleutel in Firefly**: `internal_reference` = `<rekening>|<duplicaatsleutel>`
    (stap 3). `external_id` is de referentie van de bank en is niet altijd uniek (Argenta) of
    bestaat niet (kaartafschriften); tools die een transactie terug moeten vinden (overrides van
    de classificatie) gebruiken de rijsleutel.
11. ✅ **Leningen als schuldrekening**: de beslissing ligt bij de privé-financerepo; deze repo
    voert alleen uit wat daar beslist wordt (§5).

## 2. Wie laadt wat in Firefly

Toetsvraag: **is het nodig om transacties correct te importeren?** Dan de feeder; anders de
financetool (privé-repo); wat eenmalig per server is of niet via de API kan: met de hand.

| Wat | Wie | Waarom |
|---|---|---|
| Eigen rekeningen: naam, soort/rol, IBAN, rekeningnummer, beginsaldo + datum, actief/afgesloten, kredietkaartinstellingen | **feeder** (stap 5) | zonder rekening faalt elke rij (`No Firefly asset account with IBAN …`); een volledige herlaadbeurt wordt zo herhaalbaar |
| Correctieboekingen die bij een rekening horen (bv. de Rabobank-rest) | **feeder**, zelfde bestand | deel van de rekeninghistoriek, niet van classificatie |
| Munten inschakelen die in transacties voorkomen | **feeder**, bij import (stap 3) | nodig voor `foreign_amount` |
| Tegenpartijen (uitgave-/inkomstrekeningen) | Firefly maakt ze zelf bij import | naamvarianten samenvoegen: financetool |
| Categorieën, tags, regels, regelgroepen, budgetten, terugkerende kosten, spaarpotjes | financetool | classificatie, persoonlijk |
| Firefly-versie, `enable_batch_processing`, Personal Access Token, standaardmunt EUR, root helper + sudo-regel, cron-job | **hand**, eenmalig per server, als checklist (stap 6) | de API bewaart de batch-instelling als tekst (Firefly negeert ze); zonder token geen API; sudo-regel en cron zijn TrueNAS, niet Firefly |
| Taal, datumnotatie, startpagina | jouw voorkeur, niet bijgehouden | de feeder heeft ze niet nodig |

## 3. Stappen (volgorde = commits)

Elke stap: `pre-commit`, regressietest. Wat een stap op Fintro mag veranderen, staat bij de
stap; al het andere moet nul verschillen geven.

### Stap 0 — Documentatie herschikken ✅ `781da6a`

Eerst, zodat elke volgende stap meteen op de juiste plek documenteert. Alleen verhuizen en
splitsen; geen nieuwe inhoud.

```
README.md                          mensen op GitHub: wat het is, starten, tabel "Banks" met links
AGENTS.md                          altijd ingeladen: harde regels (idle-kost, normalisatieprincipe,
                                   geen echte data), draaien, testen, importer, Firefly-checklist,
                                   Start Over, een bank toevoegen; tabel "Banks" met links
.claude/rules/architectural_patterns.md
                                   altijd ingeladen: alleen de algemene architectuur
docs/output-contract.md            waar andere tools op mogen rekenen
engine/banks/<bank>/README.md      één per bank, vaste indeling (hieronder)
engine/banks/<bank>/CLAUDE.md      één regel: @README.md
config/accounts.example.yaml       legt het rekeningbestand zelf uit (stap 5)
plans/NN-*.md                      tijdelijk (hieronder)
```

1. **Per bank `engine/banks/<bank>/README.md`**, vaste kopjes: Export (hoe je ze bij de bank
   haalt, formaat), Kolommen, Unieke rij (duplicaatsleutel en waarom), Veldtoewijzing (bank →
   intern → normalized), Parseerregels en wat expliciet wegvalt, Markers en conventies,
   Eigenaardigheden, Controleren (`debug_row`, kruiscontroles). GitHub toont die README als je de
   map opent. Claude Code laadt een `CLAUDE.md` in een submap pas in als het bestanden in die map
   leest; met `@README.md` erin wordt de bankdocumentatie dus alleen ingeladen als het over die
   bank gaat. Andere AI-tools vinden ze via de tabel "Banks" in AGENTS.md.
2. **Fintro** krijgt de eerste: uit AGENTS.md "Fintro message markers" en de regel over de
   tegenpartij `Fintro`; uit architectural_patterns de Fintro-details van §5 (twee bronnen) en §6
   (de details-kolom ontleden). Daar blijft het algemene principe: twee bronnen vergelijken, falen
   bij tegenspraak.
3. **`docs/output-contract.md`**: per normalized veld wat het betekent; de woorden voor
   transactietypes; markers; formaat van de notities; welke sleutel per bank uniek en stabiel is
   (`row_key`, `external_id`); een korte lijst van wijzigingen, zodat een tool die erop rekent
   weet wanneer hij moet aanpassen. Publiek, zodat de privé-financerepo ernaar kan linken.
4. **Plannen zijn tijdelijk**: na uitvoering gaan de blijvende feiten naar de bestanden hierboven
   en verdwijnt het plan (git bewaart het). AGENTS.md zegt dat in één regel.

### Stap 1 — Bestandsnamen in `data/` ✅

Nu: `<ts>-<originele naam>-<fase>`. Twee problemen (nagekeken in de code en op de server):
- De naam zegt niet welke rekening of periode (`CSV_2026-01-31-12.00`, verzonnen); alleen de
  indexbackups tonen de rekening.
- Niet uniek: bestanden in dezelfde seconde krijgen dezelfde `<ts>` (op de server: 5 om
  21:20:01). Twee bestanden met dezelfde naam zonder extensie (`202609.pdf`, `202609.csv`, zodra
  plan 02 andere formaten toelaat) zouden elkaars uitvoer, log en indexbackup stil overschrijven.

Nieuw: de naam beschrijft de inhoud, en is uniek door hoe hij gemaakt wordt.

```
<run>-<bank>-<rekening>-<van>_<tot>-<fase>.<ext>
```

| Deel | Wat | Verzonnen voorbeeld |
|---|---|---|
| `<run>` | starttijd van de cronrun + volgnummer van het bestand in die run (bash-teller, builtin) → **uniek zonder wachten** | `20261009-101500-001` |
| `<bank>` | uit de config | `fintro` |
| `<rekening>` | IBAN zonder spaties (of een ander rekeningnummer) | `BE68539007547034` |
| `<van>_<tot>` | eerste en laatste transactiedatum in het bestand | `2026-03-13_2026-10-07` |
| `<fase>` | zoals nu | `processed`, `normalized`, `imported-partial`, … |
| `<ext>` | het origineel houdt zijn eigen extensie; alle uitvoer is `.csv` | `.csv` |

1. **Bash** geeft alleen `<run>` en het pad aan Python. Bij een crash (Python stopt vóór
   `finalize`) verplaatst bash het origineel naar `failed/<run>-crashed-<originele naam>.<ext>`
   en schrijft in `<run>.log`. "crashed" maakt het onderscheid met `processed/…-processed-failed`
   (Python wees het bestand af).
2. **Python kiest de namen**: `build_paths` maakt eerst voorlopige namen (`<run>`), de
   definitieve pas na herkenning van bank, rekening en periode; `finalize` (het enige
   uitgangspunt) gebruikt die. De log heet tijdens de run `<run>.log` en krijgt bij `finalize`
   zijn definitieve naam. De importer leidt zijn namen (`-imported`, `-import.log`,
   `-import-failed`) zoals nu af van het normalized-bestand.
3. **De originele naam** staat op de eerste regel van de log (`Origineel: gem 260313-261007.csv`),
   en de log noemt de herkende rekening (nu enkel `Detected bank: fintro`).
4. **Bank onbekend** (afgewezen vóór herkenning): `<run>-unknown-<originele naam>-processed-failed.<ext>`
   — dan is de originele naam het enige houvast.
5. **Indexbackups**: `backups/<run>-<rekening>-duplicate-index.csv`. De rotatie leest de rekening
   als laatste deel van de naam; IBAN en klantenreferentie bevatten geen `-`.
6. Bestaande bestanden in `data/` houden hun oude naam tot de volledige herlaadbeurt (plan 02).

Fintro-regressie: nul verschillen (namen vallen buiten de test). Idle-meting herhalen.

Uitgevoerd, met drie keuzes die hierboven nog niet stonden:
- De periode komt uit `date_format` bij `primary_transaction_date` in de bank-yaml (de
  datums zijn per bank anders geschreven; `engine/core/` kent geen banknamen).
- Een run die start in een seconde die een eerdere run al gebruikte (handmatige run vlak na
  een cronrun), laat het werk aan de volgende minuut: anders zouden beide met `-001` beginnen.
- Bash noemt een origineel na een mislukte kritieke verplaatsing (exit 92–97)
  `<run>-move-failed-<naam>`, na een crash `<run>-crashed-<naam>`.
Getest in een kopie: Fintro-export, onbekend bestand, gesimuleerde crash, bezette seconde;
regressie 0 verschillen op 14.080 rijen; idle op de server 1,9 ms.

### Stap 2 — Fintro: valutadatum en vreemde munt

Apart, zodat de regressietest precies deze verschillen toont en niets anders:
1. `booking_date` → `interest_date` in `config/fintro.yaml`, `normalize.py`, `debug_row.py`,
   `NORMALIZED_FIELDNAMES` en `build_split`. Verwacht: elke Fintro-rij toont die hernoeming.
2. Nieuwe normalized velden `foreign_amount` + `foreign_currency_code` uit de bestaande
   wisselkoerstekst, met de rekencontrole van §1.3. Verwacht: de 29 rijen met een vreemd bedrag krijgen die twee velden;
   de notities blijven ongewijzigd.

Uitrollen als `data/normalized/` leeg is: een bestand dat daar nog met de oude kolomnaam
wacht, zou zijn valutadatum verliezen. **Uitrollen = opslaan** (AGENTS.md "Deploy = save"):
de SFTP-watcher zet elk opgeslagen bestand meteen op de server. Dus vóór de eerste bewerking
op de server nakijken dat `data/incoming/` en `data/normalized/` leeg zijn en er geen
`data/*.flag` wacht, en alle bestanden van deze stap snel na elkaar opslaan. Wat al in Firefly staat, houdt `book_date` tot de
herlaadbeurt (plan 02).

### Stap 3 — Importer

1. Eigen rekeningen opzoekbaar op **IBAN én rekeningnummer** van de Firefly-rekening:
   - eigen kant: `asset_account_iban`, of het nieuwe normalized veld `asset_account_number`
     (rekening zonder IBAN, zoals een kredietkaart);
   - tegenpartij: `opposing_account_iban` of `opposing_account_number`, elk gezocht in beide
     velden. Zo wordt een Fintro-rij naar de Rabobank-zicht-IBAN herkend, ook al staat die IBAN
     in het rekeningnummerveld (§1.8).
   Oude bestanden zonder `asset_account_number` werken verder; leeg veld = afwezig veld, dus
   geen verschil in de Fintro-regressie.
2. Vangnet: tegenpartij = de rekening zelf → rij faalt met duidelijke reden (nu gaat ze door
   als inkomst/uitgave met je eigen IBAN als tegenrekening; wat Firefly daarmee doet is niet getest).
3. `foreign_amount` + `foreign_currency_code` → Firefly's gelijknamige velden.
4. Munten: één keer per importrun Firefly's muntenlijst (`GET /v1/currencies`); een
   uitgeschakelde munt van een rij wordt ingeschakeld (`POST /v1/currencies/{code}/enable`,
   in de API-specificatie 6.5.5) en gelogd; een munt die Firefly niet kent laat de rij falen
   (een munt aanmaken vraagt een naam en symbool, die verzint de feeder niet).
5. Rijsleutel (§1.10): de normalizer schrijft een nieuw normalized veld `row_key` =
   `<rekening>|<duplicaatsleutel>` (in `process_csv`, niet in `normalize_row`, dus geen verschil
   in de regressietest); de importer stuurt het als `internal_reference`. Bij een overschrijving
   tussen eigen rekeningen staat alleen de rijsleutel van de eerste kant in Firefly (zoals nu
   met `external_id`); het contract (stap 0) zegt dat.

Nagaan bij de uitvoering: hoort elk verstuurd veld bij Firefly's duplicaathash? Dan geven de
nieuwe velden (`interest_date`, vreemde munt, `internal_reference`) een andere hash, en zou een
bestand dat vóór deze stappen genormaliseerd werd bij opnieuw importeren dubbel komen. Tot de
volledige herlaadbeurt (plan 02) dan geen oude bestanden opnieuw importeren.

### Stap 4 — Regressietest en `debug_row`

1. `engine/regression.py`: rijen identificeren met (rekening, **duplicaatsleutel**) i.p.v.
   `(IBAN, external_id)`, want niet elke bank heeft een unieke `external_id` (plan 02); glob
   recursief (`**`), zodat één commando alle submappen van `bank-csv-originals/` dekt.
2. Generieke `engine/debug_row.py`: kolommen, duplicaatstatus, resultaat of exacte fout; een
   bank mag een eigen stap-voor-stap-trace toevoegen. Fintro's details-trace blijft.

### Stap 5 — Eigen rekeningen via een bestand

Nu maak je rekeningen met de hand in de Firefly-GUI. Nieuw: je beschrijft ze één keer in een
bestand, en de feeder zorgt dat Firefly ermee overeenkomt.

- Vóór elke importrun: ontbrekende rekening aanmaken, gewijzigde velden bijwerken, nooit iets
  verwijderen; een eigen rekening in Firefly die niet in het bestand staat → melding.
  Alleen binnen een importrun, dus een idle run blijft gratis.
- Correctieboekingen gaan als gewone transacties met `error_if_duplicate_hash`: het bestand
  opnieuw toepassen boekt ze niet dubbel.
- Het bestand is de waarheid: een GUI-wijziging aan zo'n rekening wordt overschreven.
- Persoonlijke gegevens → **niet in deze publieke repo**. De repo krijgt een sjabloon
  `config/accounts.example.yaml` met alle uitleg en verzonnen rekeningen (Engels, zoals de rest
  van de repo); je eigen bestand staat in de privé-financerepo en begint als kopie daarvan.
  `config/app.env` zegt waar het op de server staat.
- Nagaan bij de uitvoering: weigert Firefly een eigen rekening met een IBAN die al bestaat als
  uitgave-/inkomstrekening (vandaag zo voor de Argenta- en Rabobank-IBAN's)? Dan is de volgorde
  bij een herlaadbeurt: Firefly wissen → rekeningbestand toepassen → alles laden.

Ontwerp van het sjabloon:

```yaml
# Own accounts in Firefly III, kept in line by firefly-iii-feeder.
#
# What the feeder does with this file, before every import run (never on an idle cron run):
#   - an account listed here but missing in Firefly is created;
#   - a field that differs from Firefly is updated in Firefly;
#   - nothing is ever deleted; an own account in Firefly that is not listed here is reported.
# This file is the truth: a change made in the Firefly GUI to a listed account is overwritten.
# An account is found in Firefly by its iban, else by its account_number.
#
# Fields (one list item per account). Between [brackets]: the field as Firefly's account form
# shows it (English GUI; a translated GUI shows the translation).
#   name                  Required. [Name]
#   kind                  Required. What the account is. [Account role]
#                           checking     current account (zichtrekening)     [Default asset account]
#                           savings      savings account or term deposit     [Savings account]
#                                        (spaarrekening, termijndeposito)
#                           credit_card  credit card with its own statement  [Credit card]
#                                        (needs payment_day)
#                           cash         cash wallet                         [Cash wallet]
#                         A debit card is not an account: it belongs to its checking account.
#   shared                true for an account of more than one person (gemeenschappelijke
#                         rekening). Only with kind checking: the role becomes
#                         [Shared asset account]. Default false.
#   iban                  IBAN, spaces allowed. Bank rows are matched to the account on it. [IBAN]
#   account_number        Second identifier, free text: an account without an IBAN (card
#                         customer reference, term deposit number), or a second IBAN whose rows
#                         belong on this same account. [Account number]
#   payment_day           credit_card only: day of the month the card balance is debited.
#                         [Credit card monthly payment date]; the feeder also sets
#                         [Credit card payment plan] to [Full payment every month].
#   opening_balance       Balance just before the first imported row (negative for a card that
#   opening_balance_date  was owed money). Both or neither. Default 0.
#                         [Opening balance], [Opening balance date]
#   active                false for a closed account. Default true. [Active]
#   corrections           Bookings no export contains (e.g. interest of a closed account without
#                         exports). Each: date, amount (+ in, - out), counterparty (name),
#                         description. Imported like bank rows, so applying the file again does
#                         not book them twice. Not an account field: ordinary transactions.

- name: ACME current account
  kind: checking
  iban: BE68 5390 0754 7034
  opening_balance: 1234.56
  opening_balance_date: 2016-01-01

- name: ACME joint account
  kind: checking
  shared: true
  iban: NL91ABNA0417164300

- name: ACME Mastercard
  kind: credit_card
  payment_day: 14
  iban: DE89 3704 0044 0532 0130 00  # account the card balance is collected on: makes the
                                     # monthly settlement from the current account a transfer
  account_number: "1234567890"       # customer reference on the card statement

- name: Old savings (closed)
  kind: savings
  iban: GB82 WEST 1234 5698 7654 32
  account_number: GB33 BUKB 2020 1555 5555 55  # the bank's current account: its rows land here too
  active: false
  corrections:
    - {date: 2020-12-31, amount: 123.45, counterparty: "Old bank", description: "Rest, presumably interest"}
```

Vertaling: wat je in het bestand schrijft, wat je in Firefly ziet, wat de feeder naar de API
stuurt (de API kent geen andere waarden). GUI-namen uit Firefly's eigen tekstbestanden
(`resources/lang/en_US/firefly.php`, `form.php`); Firefly's Nederlandse vertaling staat niet in
de broncode, dus die namen zijn niet nagekeken.

| `kind` (+ `shared`) | Firefly-GUI, "Account role" | API `account_role` | Extra die Firefly dan vraagt |
|---|---|---|---|
| `checking` | Default asset account | `defaultAsset` | — |
| `checking` + `shared: true` | Shared asset account | `sharedAsset` | — |
| `savings` (ook termijndeposito) | Savings account | `savingAsset` | — |
| `credit_card` | Credit card | `ccAsset` | payment plan "Full payment every month" (`monthlyFull`), monthly payment date (uit `payment_day`) |
| `cash` | Cash wallet | `cashWalletAsset` | — |

"Debit" is geen soort rekening maar een kaart bij een zichtrekening; die kaart heeft in
Firefly geen eigen rekening.

**Hoe het bestand op TrueNAS komt.** De financerepo staat alleen op de desktop (+ Forgejo).
- **Gekozen: SFTP vanaf de desktop**, zoals de feedercode zelf: de financerepo krijgt een
  `sftp.json` die alleen dat bestand uploadt bij opslaan. Niets nieuws te beveiligen, geen
  extra cron. Nadeel: de server krijgt wat opgeslagen is, ook als het nog niet gecommit is.
- **Later, als dat nadeel echt hindert — TrueNAS haalt het uit Forgejo** (dat draait op dezelfde
  server): de cron-gebruiker krijgt een alleen-lezen deploy key voor de financerepo en doet
  `git pull` aan het begin van een importrun (nooit idle). De server krijgt dan alleen wat
  gepusht is. Kost: een extra sleutel, git in de importrun, en als Forgejo net down is gaat de
  import verder met de vorige versie van het bestand. Komt in de documentatie (stap 6).

### Stap 6 — Documentatie

In de structuur van stap 0 (AGENTS.md, README, architectural_patterns.md, Fintro-README,
output-contract):
- bestandsnamen in `data/` (ook architectural_patterns §10 "Timestamp-Everything");
- `interest_date`, vreemde munt, munten inschakelen, eigen rekeningen op IBAN of rekeningnummer,
  `row_key` / `internal_reference` (ook in het contract);
- regressiecommando recursief; generieke `debug_row`;
- rekeningbestand: sjabloon, plaats op de server, SFTP en de Forgejo-optie;
- nieuwe sectie **"Firefly setup (by hand, once per server)"**, alles op één plek (nu verspreid
  over de gotchas en "Root helper"), alleen algemene stappen:
  1. Firefly ≥ 6.7.0.
  2. Admin → Configuration → `enable_batch_processing` aan (in de GUI).
  3. Personal Access Token aanmaken → `FIREFLY_TOKEN` in `config/app.env` (verloopt na max. 1 jaar).
  4. Standaardmunt EUR (kan ook via de API, `/v1/currencies/{code}/primary`; eenmalig, dus met de hand).
  5. Root helper installeren + sudo-regel voor de cron-gebruiker.
  6. Cron-job in TrueNAS (elke minuut, lock-guard, "Hide Standard Error" uit).
- idle-meting herhalen en het getal bijwerken als het verandert.

## 4. Testen

1. Regressietest na elke stap: op Fintro verandert alleen stap 2 iets (zoals daar beschreven).
2. Server: een Fintro-CSV via `data/incoming/` → namen, log en uitvoer nakijken in
   `data/processed/`, `data/normalized/`, `data/logs/`; importer met `--dry-run`.
3. Rekeningbestand: eerst met `--dry-run` (toont wat het zou aanmaken/bijwerken), dan echt.
4. Voor elke commit: staged diff scannen op IBAN-vormige strings en echte namen (publieke repo).

## 5. Later

- **Leningen** als schuldrekening, als de privé-financerepo daartoe beslist: kind `loan` in het
  rekeningbestand, afbetalingen als overschrijving naar die rekening. Kapitaal vs. interest
  vraagt dan een eigen ontwerp.
- In de bestandsnamen een korte rekeningnaam uit het rekeningbestand in plaats van de IBAN.
