# Zeleninur — testovací nasazení

Webová hra Zeleninur. Tajné hodnoty (ZELENINUR_SECRET, ZELENINUR_ADMIN_PASSWORD) patří výhradně do soukromých Environment Variables v hostingu a nikdy do tohoto repozitáře.

## Nasazení na Railway

Po nahrání obsahu aplikace z balíčku `Zeleninur_web_deploy` do kořene tohoto repozitáře připojte repozitář k Railway a vytvořte veřejnou doménu. Nastavte proměnné `ZELENINUR_DATA_DIR=/data`, `ZELENINUR_DB_PATH=/data/zeleninur.db`, `ZELENINUR_SECRET` (náhodná dlouhá hodnota), `ZELENINUR_ALLOW_DEMO_USERS=false`, `ZELENINUR_ADMIN_USERNAME` a `ZELENINUR_ADMIN_PASSWORD`. Připojte persistent volume na `/data` a nechte jednu repliku.

**Pozor:** Tento repozitář je veřejný. Nikdy sem necommitujte databázi, `.env`, autentizační tokeny ani skutečná hesla.
