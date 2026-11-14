# Yerelde Çalıştırma ve Test (telefonsuz)

Bu dosya "elimde telefon yok, istek nasıl atarım" sorusunun cevabı. Üç yol var;
hiçbiri telefon/DevTools istemez.

---

## 0) Tek dosya, klon bile yok (en hızlı)

```bash
mkdir -p ~/awtest && cd ~/awtest
curl -fsSL -O https://raw.githubusercontent.com/keremtulu44/ApiWrapper/arena/b6435933-apiwrapper/scripts/smoke.py
python3 -m pip install --quiet httpx

python3 smoke.py --base https://SERVIS.onrender.com --key "$API_KEY" --admin-key "$API_KEY"
```

Çıktı: hangi adımda takıldığı + katmanı + ilk düzeltme. Takılınca çıkış kodu `1`.

---

## 1) Depodan çalıştırma (tam kurulum)

```bash
git clone https://github.com/keremtulu44/ApiWrapper.git
cd ApiWrapper
git checkout arena/b6435933-apiwrapper     # duman testi bu dalda

python3 -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# sunucuya karşı (yerelde sunucu açmadan)
python -m scripts.smoke --base https://SERVIS.onrender.com --key "$API_KEY"
python -m scripts.smoke --base https://SERVIS.onrender.com --key "$API_KEY" --stream
python -m scripts.smoke --base https://SERVIS.onrender.com --key "$API_KEY" --json | jq
python -m scripts.smoke --base https://SERVIS.onrender.com --key "$API_KEY" --skip-chat
```

`--admin-key` vermezsen `--key` değeri teşhis için de kullanılır.

---

## 2) Kendi makinende sunucu açıp ona karşı test

```bash
cp env.mobile.example .env      # sonra §1'deki 5 satırı doldur
python -m scripts.env_audit --env .env      # eksik/ölü/yer tutucu anahtar taraması
python -m scripts.env_template --check      # şablon güncel mi

uvicorn app.main:app --reload --port 8000
```

İkinci terminal:

```bash
python -m scripts.smoke --base http://127.0.0.1:8000 --key "$(grep '^API_KEYS=' .env | cut -d= -f2)"
```

Testler:

```bash
pytest -q                 # tamamı
pytest -q tests/test_smoke.py
ruff check app tests scripts && mypy app
```

---

## 3) tek satır `curl` karşılıkları

```bash
BASE=https://SERVIS.onrender.com ; KEY=$API_KEY

curl -s $BASE/healthz | jq
curl -s $BASE/readyz  | jq
curl -s $BASE/v1/admin/auth -H "Authorization: Bearer $KEY" | jq .verdict
curl -s $BASE/v1/admin/config -H "Authorization: Bearer $KEY" | jq

# gerçek istek (akışsız)
curl -s $BASE/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"max","messages":[{"role":"user","content":"ping"}],"max_tokens":8}' | jq

# akış (ilk olaylar)
curl -sN $BASE/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"max","messages":[{"role":"user","content":"ping"}],"stream":true}' | head -5

# çerezi uygulamadan dene (hiçbir şeyi değiştirmez)
curl -s -X POST "$BASE/v1/admin/upstream/cookie?dry_run=true" \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"cookie":"<çerez>"}' | jq

# tarayıcıyla damga/cx tazele (giriş yapmaz)
curl -s -X POST $BASE/v1/admin/upstream/capture -H "Authorization: Bearer $KEY" | jq
```

---

## Çıktıyı okuma

| Kod | Katman | İlk bakılacak |
|---|---|---|
| 401 | identity | `UPSTREAM_AUTH_FROM_COOKIE`, token/çerez tazeliği |
| 403 | captcha | `RECAPTCHA_*`, `UPSTREAM_USER_AGENT`, çerez |
| 404 | config | `UPSTREAM_STREAM_PATH` |
| 409 | deployment | `UPSTREAM_DEPLOYMENT` / damga |
| 429 | kota | hesap havuzu, `UPSTREAM_LIMIT_MARKERS` |
| 502 | config | `TARGET_DOMAIN`, şema, DNS |
| bağlantı yok | ağ | adres, Render uykusu, VPN |

## Notlar

- `/v1/admin/*` **önce** `API_KEYS` doğrular: `ADMIN_API_KEYS`'e yazdığın anahtar
  `API_KEYS`'te de olmalı, yoksa 401 (açılışta `admin_keys_not_in_api_keys` uyarısı).
- Duman testi gizli değer basmaz; anahtarları `abc…yz` biçiminde maskeler.
- Render ücretsiz planda servis uyur: ilk istek 30–60 sn sürebilir, `timeout` 60 sn.
