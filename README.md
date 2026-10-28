# MI-vezérelt IT-folyamatautomatizáció ServiceNow-integrációval

Szakdolgozati projekt. Cél egy mikroszolgáltatás, amely a ServiceNow incidenseket nagy nyelvi modell (Claude) segítségével besorolja, priorizálja és a megfelelő csoporthoz irányítja, a lezárásról pedig nem a modell, hanem determinisztikus szabályok döntenek.

## Probléma és cél

Az IT-ügyfélszolgálat első vonala sok ismétlődő, szöveges bejelentésből dönt: melyik kategória, mennyire sürgős, ki oldja meg, van-e ismert megoldás. A nyelvi modell ezt gyorsan el tudja végezni, de a kimenete valószínűségi, a bemenete pedig megbízhatatlan, mert a bejelentés szövegét bárki megírhatja.

A projekt célja olyan rendszer tervezése és mérése, amelyben a modell csak javaslatot tesz, az automatikus beavatkozást pedig szigorú, tesztelhető feltételek korlátozzák.

## Kutatási kérdések

1. Hogyan építhető ServiceNow-integrált, LLM-alapú triázs úgy, hogy az automatikus lezárás ne csak a modell állításán múljon?
2. Mely védőkorlátok szükségesek, és hogyan tesztelhetők egymástól függetlenül?
3. Milyen mérőszámokkal értékelhető az automatizálás kockázata?

## Tervezett felépítés

```
ServiceNow --(webhook)--> szolgáltatás --> modell (javaslat) --> kapuk --> visszaírás
                                |                                          |
                                +----------- auditnapló, metrikák ---------+
```

Tervezett autonómiaszintek:

| Üzemmód | Tervezett működés |
| --- | --- |
| `shadow` | Csak elemez és naplóz. |
| `assist` | Kategóriát, sürgősséget és csoportot állít, nem zár le. |
| `autonomous` | Szigorú feltételek teljesülése esetén lezár. |

## Tervezett könyvtárszerkezet

```
.
├── README.md
├── requirements.txt
├── Dockerfile
├── .env.example
├── config.py          konfiguráció
├── models.py          adatmodellek
├── servicenow.py      Table API kliens
├── ai_triage.py       nyelvi modell hívása, tartalék osztályozó
├── gates.py           az automatikus lezárás feltételei
├── security.py        bemenet-védelem, aláírás-ellenőrzés
├── audit.py           auditnapló, idempotencia
├── automation.py      a folyamat összekötése
├── main.py            webszolgáltatás
├── evaluation.py      kiértékelés
├── eval_dataset.json  címkézett minták
└── test_*.py          tesztek
```

## Ütemterv

- [x] Témaválasztás, kutatási kérdések rögzítése
- [x] Projektváz és könyvtárszerkezet
- [ ] ServiceNow Table API kliens
- [ ] Nyelvi modell alapú triázs, strukturált kimenettel
- [ ] Az automatikus lezárás feltételrendszere (kapuk)
- [ ] Bemenet-védelem és webhook-hitelesítés
- [ ] Auditnapló és ismétlés elleni védelem
- [ ] Kiértékelő keretrendszer és címkézett minták
- [ ] Tesztek
- [ ] Mérés valódi nyelvi modellel és ServiceNow-példánnyal
- [ ] Szakdolgozat írása

## Gyorsindítás (fejlesztéshez)

Python 3.11 vagy újabb szükséges.

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

A `.env` fájlt ne töltsd fel a tárolóba, csak a `.env.example` kerüljön be.

## Megjegyzés

A projekt oktatási célú. Éles használat előtt a rendszert saját adaton, `shadow` üzemmódban ki kell próbálni.
