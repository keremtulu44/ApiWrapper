# `/auth/v1/token` 401 döngüsü — ne olduğu ve telefonsuz çözümü

Canlı tail'de dakikada bir tekrar eden satırlar:

```json
{"status_code": 401, "endpoint_path": "/auth/v1/token", "slot": 1,
 "event": "upstream_token_refresh_rejected", "level": "warning"}
{"expires_in_seconds": 583, "reason": "Upstream `apikey` istiyor: ...",
 "event": "access_token_expiring_soon", "level": "warning"}
{"ok": false, "expires_in": null, "event": "token_refresher_cycle", "level": "info"}
```

## Bunlar senin isteğin değil

`token_refresher_cycle` arka plan döngüsüdür: `UPSTREAM_REFRESH_CHECK_INTERVAL_SECONDS`
(varsayılan 60 sn) aralığında çalışır. Çerezde `refresh_token` var ve erişim
token'ının ömrü `UPSTREAM_REFRESH_LEEWAY_SECONDS` (600 sn) eşiğinin altına
indiği için **her turda** yenileme deniyor. `GET /` satırları Render'ın sağlık
kontrolü, onlar da senin trafiğin değil.

Yani "istek atmadım ama log akıyor" normal. Akışın sebebi: yenileme başarısız
olduğu için eşik altında kalıyor ve döngü her dakika tekrar deniyor.

## 401'in sebebi: eksik `apikey` başlığı

Upstream bir **Supabase/GoTrue** kimlik sunucusu. GoTrue, `/auth/v1/token`
çağrısında projenin **anon/publishable** anahtarını `apikey` başlığında ister;
yoksa gövdesi `{"message":"No API key found in request"}` olan **401** döner.
Kod bunu tanıyor (`reason: missing_apikey`), otomatik keşif deniyor, bulamayınca
da o cümleyi yazıyor.

Dikkat: `refresh_token` **geçersiz** olsaydı kod `400 invalid_grant` /
`Invalid Refresh Token` görür ve "taze çerez gerekir" derdi. Log'da 401 + apikey
cümlesi var → sorun çerezde değil, başlıkta.

## Çözüm (deploy yok, yeniden başlatma yok)

```bash
BASE=https://SERVIS.onrender.com
KEY=<API_KEYS değeri>

curl -s -X POST $BASE/v1/admin/upstream/apikey \
  -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"apikey":"<SUPABASE_ANON_KEY>"}' | jq
```

Cevap yalan söylemez, aynı istekte yenilemeyi de dener:

```json
{ "status": "applied", "refreshed": true, "expires_in": 3600,
  "refresh_detail": "Erişim token'ı yenilendi." }
```

`refreshed: true` ise oturum tazelendi; `false` ise `refresh_detail` hangi
adımın reddedildiğini söyler (ör. `refresh_token_already_used` → çerez gerekir).

PowerShell karşılığı:

```powershell
$body = @{ apikey = "<SUPABASE_ANON_KEY>" } | ConvertTo-Json
Invoke-RestMethod "$BASE/v1/admin/upstream/apikey" -Method Post -Headers $H `
  -ContentType "application/json" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

Kalıcı olsun diye `.env` / Render env'e de yaz (uç ayrıca render env'i senkronlar):

```dotenv
UPSTREAM_REFRESH_API_KEY=<SUPABASE_ANON_KEY>
UPSTREAM_REFRESH_URL=https://<proje>.supabase.co/auth/v1/token?grant_type=refresh_token
```

`UPSTREAM_REFRESH_URL` boş bırakılabilir: çerezdeki JWT'nin `iss` alanından
türetilir.

Anon anahtarı nereden: Supabase Dashboard → Project Settings → API →
`anon` / `public` anahtar. Bu anahtar tarayıcıya giden **public** anahtardır;
`service_role` anahtarını buraya koymayın.

## Yenileme çalıştıktan sonra ne değişir

- `token_refresher_cycle` satırında `"ok": true` ve `expires_in: 3600` görünür.
- Yeni `refresh_token` (rotation) hem bellekteki ayarlara hem
  `UPSTREAM_STATE_FILE` dosyasına yazılır; Render restart'ında bu dosya yoksa
  env'deki çerez taban kabul edilir.
- 401 döngüsü biter; servis 1 saatlik dilimlerde kendini tazeler.

## Hâlâ 401 alıyorsan sırayla

| Log'da görünen | Anlamı | Yapılacak |
|---|---|---|
| `reason: missing_apikey` | anahtar hiç gönderilmedi | `/v1/admin/upstream/apikey` |
| `reason: invalid_apikey` | anahtar gönderildi, **bu projeye ait değil** | o hostun kendi anon anahtarı (`upstream_message` + `url_host`) |
| `reason: invalid_refresh_token` | çerezdeki refresh token geçersiz | taze çerez (`/v1/admin/upstream/cookie`) |
| `reason: refresh_token_already_used` | rotation: eski token ikinci kez kullanıldı | taze çerez; `UPSTREAM_STATE_FILE` kalıcı olsun |
| `reason: rate_limited` | çok deneme | geri çekilme aralığı büyür, 5 dk'yı bekle |
| `status_code: 200` ama `ok: false` | gövdede `access_token` yok | yanıt biçimi; `UPSTREAM_REFRESH_URL` doğru mu |

## Bu turda koda eklenenler

- Red logu artık nedeni de yazıyor: `reason`, `apikey_configured`,
  `apikey_discovered` — tek bakışta hangi düzeltme gerektiği belli.
- `token_refresher_cycle` satırına `consecutive_failures` eklendi.
- Red loguna `upstream_message` eklendi (JWT/base64 blokları `<deger>` ile maskelenir):
  GoTrue'nun "No API key found" ile "Invalid API key" cümlesi artık ayrı kodlara düşüyor.
- `/v1/admin/config` → `upstream_refresh`: host, yol, apikey uzunluğu/öneki, son durum
  ("anahtar yazdım ama yine apikey istiyor" için ilk bakılacak yer).
- Kalıcı hatada döngü geri çekiliyor: 60 → 120 → 240 → 300 sn (tavan). Böylece
  aynı 401 dakikada bir tekrarlanıp logu ve upstream'in
  `rate_limit_token_refresh` kotasını doldurmuyor.
- Kimlik değiştiren uçlar (`cookie`, `curl`, `apikey`, `capture`, `refresh`)
  sayacı sıfırlıyor: düzeltmeden sonra beklemeden deneme yapılır.
