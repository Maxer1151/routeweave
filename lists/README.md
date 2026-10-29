# lists/ — списки доменов

Списки для rule-provider mihomo. Формат: `+.domain` — домен и все поддомены,
одна запись на строку, `#` — комментарий.

| Файл | Источник | Кто пишет | Лицензия |
|---|---|---|---|
| `geo.txt` | `Internet-Helper/Unblock-for-Russia` (`geoblock.lst`) | генерируется таймером | не MIT, см. [`../NOTICE`](../NOTICE) |
| `ru-direct.txt` | `lib4u/amnezia-tunneling-ru` (`amnezia.json`) | генерируется таймером | не MIT, см. [`../NOTICE`](../NOTICE) |
| `geo-custom.txt` | собственный | вручную или из панели | MIT |
| `ru-direct-custom.txt` | собственный | вручную или из панели | MIT |
| `dpi.txt` | собственный | вручную или из панели | MIT |

`geo.txt` и `ru-direct.txt` — производные сторонних источников и **не
покрываются MIT**; права остаются у источников. Подробности и цепочки
источников — в [`../NOTICE`](../NOTICE).

## Обновление

`geo.txt` и `ru-direct.txt` генерирует `/usr/local/sbin/routeweave-update-lists.sh`
по таймеру `routeweave-lists-update.timer` (ежедневно в 04:15). Ручной прогон
и журнал:

```bash
sudo systemctl start routeweave-lists-update.service
journalctl -u routeweave-lists-update -n 50
```

Файлы в этом каталоге — исходные копии: установщик копирует `*.txt` в
`/etc/routeweave/lists/`, не перезаписывая уже существующие.

Сгенерированные файлы руками не правятся: следующее обновление их
перезапишет, а комментарии-заголовки из них исчезнут. Свои домены добавляйте
в `geo-custom.txt`, `ru-direct-custom.txt` или `dpi.txt` — в формате
`+.domain`.

Приоритет: `dpi.txt` (zapret) > `geo-custom.txt` (SOCKS) >
`ru-direct-custom.txt` (direct) > сгенерированные `geo.txt` / `ru-direct.txt`.
