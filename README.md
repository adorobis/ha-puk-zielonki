# PUK Zielonki for Home Assistant

[![Marketplace][marketplace-badge]][marketplace-url]
[![Validate][validate-badge]][validate-url]

Unofficial Home Assistant integration that reads the water meter readings from the invoice
e-mails of **PUK Zielonki** (Przedsiębiorstwo Usług Komunalnych w Zielonkach). It reads the
mails over IMAP, parses the PDF invoice attached to each one and creates sensors and
long-term statistics. The statistics carry the reading dates printed on the invoice, not
the date the e-mail arrived.

> **Not affiliated with PUK Zielonki.** It parses the invoices as they are formatted today.
> If the layout changes, parsing may stop working until the integration is updated.

[Po polsku ↓](#po-polsku)

## Features

- **Reads your mailbox** over IMAP (read-only: messages are not marked as read) and picks up
  every invoice notification from `ibo@puk-zielonki.pl`, including old ones. Each message
  is downloaded and parsed once and then kept in a local cache.
- **Several properties**: invoices are grouped by the customer number ("numer
  ewidencyjny"). Each property gets its own device. A property that sends its first
  invoice later is added automatically.
- **Main and garden meter**: the meter that is also billed for sewage ("Ścieki") is the
  main meter. A meter billed for water only is the garden sub-meter. If an invoice has no
  sewage lines at all, its meter counts as the main meter.
- **Sensors** per property:
  - main meter and garden meter reading (m³), with the reading date, previous reading,
    consumption and the last 12 readings as attributes
  - reading date
  - latest invoice amount (PLN), with its number, issue date, due date and the property's
    address as attributes
  - payment due date
- **Long-term statistics** (usable in the Energy dashboard → Water and in statistics graph
  cards):
  - `puk_zielonki:<property>_main_meter`, `puk_zielonki:<property>_garden_meter` (m³)
  - `puk_zielonki:<property>_cost` (PLN)

  Each reading is stored on its reading date (12:00 local time). The consumption between
  two readings is spread evenly over the days in between, so monthly charts show a bar for
  every month. The values on the reading dates are exact and the days in between are an
  estimate. Invoice amounts are spread the same way. A missing invoice does not lose
  consumption, because the next reading is counted from the last known one.
- **Event** `puk_zielonki_new_invoice` with the parsed invoice when a new one arrives.
- **Comparison with your own meter** (optional): pick a reference sensor per meter, for
  example the total of an ESPHome pulse counter. An **offset** sensor then shows how far
  the reference sensor was from the invoice on the last reading date, and how the
  consumption of the billing period compares (see below).

## Requirements

- Home Assistant 2026.11 or newer.
- E-mail invoice notifications enabled in the PUK Zielonki customer portal (IBO).
- IMAP access to that mailbox. For Gmail use an
  [app password](https://myaccount.google.com/apppasswords).

## Installation

### Marketplace (recommended)

Install it from the Home Assistant **Marketplace** as a custom repository:

1. Settings → **Marketplace**. Adding custom repositories needs the Marketplace to be
   connected to GitHub; it asks for that the first time.
2. ⋮ → **Custom repositories** → add `https://github.com/adorobis/ha-puk-zielonki`, type
   **Integration**.
3. Open **PUK Zielonki** and **Download** it.

Or open the repository in your Marketplace directly:

[![Open in your Home Assistant Marketplace][my-badge]][my-url]

### Manual

Copy `custom_components/puk_zielonki` into your `config/custom_components/` folder and
restart Home Assistant.

## Configuration

Settings → Devices & services → **Add integration** → **PUK Zielonki**:

| Field | Meaning |
|---|---|
| IMAP server, port, username | Pre-filled from the core IMAP integration if you have one. |
| Password | Leave empty to reuse the password of a core IMAP entry with the same server and username. |
| Folder | Leave empty to use Gmail's "All Mail" (any server's `\All` folder), or INBOX. Set it if your invoices are filed in a specific folder. |
| Sender | `ibo@puk-zielonki.pl` |

The mailbox is checked every 6 hours. If the password stops working, Home Assistant asks
for a new one.

## Comparing with a pulse meter

Integration options (Settings → Devices & services → PUK Zielonki → **Configure**) list one
field per meter found on the invoices, named `reference_<property>_<meter>`. Pick the
sensor that counts the same water, for example the total of an ESPHome pulse meter. It
needs long-term statistics, so it must have a `state_class` (`total` or
`total_increasing`).

For every reading on the invoices, the reference sensor's statistics are read at 12:00 on
the reading date. The invoice has no time of day, so this is an approximation. A
"Main meter offset" / "Garden meter offset" sensor then shows:

- **state**: sensor value minus invoice reading on the latest reading date. The invoice
  reading is a whole number and the reading time is unknown, so about ±1 m³ is normal. A
  growing offset means the pulse meter needs recalibrating.
- **attributes**: the readings from both sources, the consumption of the billing period
  from both (`invoice_consumption`, `sensor_consumption`, `consumption_diff`,
  `consumption_diff_pct`), and `history` with the last 12 reading dates.

The period consumption does not depend on the pulse meter's calibration, so it is the
better measure of how accurate the pulse meter is.

## Notes

- The meter sensors have no `state_class` on purpose. The invoice e-mail arrives about a
  week after the meter is read, so statistics recorded from the sensor state would be a
  week late. Use the `puk_zielonki:*` statistics instead.
- Statistics are rebuilt from all cached invoices when Home Assistant starts and when a new
  invoice arrives.
- The garden meter is a sub-meter of the main meter. In the Energy dashboard add only the
  main meter, or both as a split, so the garden water is not counted twice.

## Privacy

The IMAP password is stored in the config entry like in the core IMAP integration. The
integration only searches for messages from the configured sender and opens the folder
read-only. Parsed invoices (numbers, amounts, readings, the property address) are cached in
`.storage/puk_zielonki.<entry_id>`. Nothing is sent anywhere else.

## Po polsku

Nieoficjalna integracja Home Assistant, która z maili z fakturami **PUK Zielonki** (IBO)
odczytuje stany wodomierzy. Łączy się ze skrzynką przez IMAP tylko do odczytu, czyta
załączoną fakturę PDF i tworzy:

- dla każdej nieruchomości (numer ewidencyjny) osobne urządzenie z sensorami: licznik
  główny, licznik ogrodowy (podlicznik), data odczytu, kwota ostatniej faktury, termin
  płatności;
- statystyki długoterminowe `puk_zielonki:<nieruchomość>_main_meter`, `_garden_meter`
  i `_cost` do panelu Energia (Woda) i kart wykresów statystyk.

Odczyty w statystykach mają datę z faktury (koniec okresu rozliczeniowego), a nie datę
przyjścia maila. Zużycie między odczytami jest rozłożone równo na dni, więc wykres miesięczny
pokazuje każdy miesiąc. Wartości w dniach odczytu są dokładne, dni pomiędzy to szacunek.

Opcjonalnie w opcjach integracji (**Konfiguruj**) można wskazać dla każdego licznika
czujnik referencyjny, np. sumę z licznika impulsowego ESPHome. Sensor „Odchyłka licznika”
pokazuje różnicę między tym czujnikiem a odczytem z faktury w dniu odczytu, a w atrybutach
porównanie zużycia w okresie rozliczeniowym i historię.

Instalacja: Ustawienia → **Marketplace** → ⋮ → **Custom repositories** →
`https://github.com/adorobis/ha-puk-zielonki` (typ **Integration**) → **Download**, potem
Ustawienia → Urządzenia i usługi → **Dodaj integrację** → **PUK Zielonki**. Puste hasło
oznacza użycie hasła z integracji IMAP dla tego samego serwera i użytkownika. Pusty folder
oznacza Gmailowe „Wszystkie” (lub INBOX).

## License

MIT

[marketplace-badge]: https://img.shields.io/badge/Marketplace-custom%20repository-41BDF5.svg
[marketplace-url]: https://www.home-assistant.io/integrations/marketplace
[validate-badge]: https://github.com/adorobis/ha-puk-zielonki/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/adorobis/ha-puk-zielonki/actions/workflows/validate.yml
[my-badge]: https://my.home-assistant.io/badges/marketplace_repository.svg
[my-url]: https://my.home-assistant.io/redirect/marketplace_repository/?owner=adorobis&repository=ha-puk-zielonki&category=integration
