#!/bin/sh
# CI'nın `image` işi bu betiği ÜRETİM İMAJININ İÇİNDE çalıştırır:
#
#   docker run --rm -e GITHUB_ACTIONS=true -v "$PWD:/work:ro" apiwrapper:ci \
#       sh /work/scripts/ci_image_tests.sh
#
# Neden: testlerin geri kalanı CI'da başka bir Python yapısında koşar. Upstream'e
# gidenler yorumlayıcıya da bağlıdır (çerez sırası 3.10'da farklıydı — LESSONS
# §20.6); gönderme sözleşmesi (tests/test_wire_contract.py) ancak Render'ın
# çalıştıracağı imajın içinde koşunca o imaj için kanıt olur.
#
# İmaj değişmez: test araçları yalnız bu geçici konteynere, kullanıcı dizinine
# kurulur; depo salt-okunur bağlıdır.
set -eu

cd /work

# 1. Sınanan kod, imaja kopyalanan kodla aynı mı?
diff -r app /app/app
diff -r config /app/config

# 2. Test araçları: pyproject.toml'daki `dev` eki. Kilit yine kısıttır; araçlar
#    kilitli hiçbir paketi değiştiremez.
python - > /tmp/dev-requirements.txt <<'EOF'
import tomllib

with open("pyproject.toml", "rb") as handle:
    print("\n".join(tomllib.load(handle)["project"]["optional-dependencies"]["dev"]))
EOF
pip install --quiet --user --no-warn-script-location \
    -c /app/requirements.lock -r /tmp/dev-requirements.txt

# 3. Kilitli paketler hâlâ yerinde ve aynı sürümde mi?
python /app/scripts/verify_lock.py /app/requirements.lock --allow-extra

# 4. Bütün testler, imajın yorumlayıcısı ve kütüphaneleriyle.
exec python -m pytest -q -p no:cacheprovider "$@"
