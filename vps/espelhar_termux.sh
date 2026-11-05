#!/bin/sh
# Rode NA VPS (cd /opt/xiaomihome && sh espelhar_termux.sh): baixa os APKs oficiais do Termux do GitHub para
# ponte/apk/, confere o sha256 publicado pelo GitHub e grava apks.json. O assistente da ponte (/configurar-ponte)
# passa a oferecer o download direto do site. Todos vêm do GitHub: app e plugins precisam da mesma assinatura.
set -e
cd "$(dirname "$0")"
mkdir -p ponte/apk
python3 - <<'PY'
import hashlib, json, urllib.request
pasta = "ponte/apk/"
alvos = {"termux": ("termux-app", "arm64-v8a.apk"), "termux32": ("termux-app", "armeabi-v7a.apk"),
         "boot": ("termux-boot", ".apk"), "api": ("termux-api", ".apk")}
saida = {}
for chave, (repo, fim) in alvos.items():
    rel = json.load(urllib.request.urlopen(f"https://api.github.com/repos/termux/{repo}/releases/latest", timeout=30))
    a = next(x for x in rel["assets"] if x["name"].endswith(fim))
    print(f"baixando {a['name']} ({a['size'] / 1e6:.1f} MB)")
    dados = urllib.request.urlopen(a["browser_download_url"], timeout=300).read()
    sha = hashlib.sha256(dados).hexdigest()
    # o Termux publica um arquivo de checksums em cada versão (o campo "digest" do GitHub vem vazio nelas)
    soma = next((x for x in rel["assets"] if "sha256" in x["name"].lower()), None)
    oficiais = urllib.request.urlopen(soma["browser_download_url"], timeout=30).read().decode() if soma else ""
    if a.get("digest"):
        oficiais += f"
{a['digest'][7:]}  {a['name']}"
    if f"{sha}  {a['name']}" not in oficiais and f"{sha} *{a['name']}" not in oficiais:
        raise SystemExit(f"sha256 de {a['name']} não confere com o publicado pelo Termux")
    open(pasta + a["name"], "wb").write(dados)
    saida[chave] = {"arquivo": a["name"], "mb": round(len(dados) / 1e6, 1), "versao": rel["tag_name"], "sha256": sha}
json.dump(saida, open(pasta + "apks.json", "w"), indent=1)
print("ok:", ", ".join(f"{k} {v['versao']}" for k, v in saida.items()))
PY
