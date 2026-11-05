#!/usr/bin/env bash
# Configura os segredos da stack: senha do painel, usuário/token do ntfy e chave da API da Claude.
# Rode na VPS com terminal interativo:  ssh -t xiaomihome /opt/xiaomihome/configurar.sh
set -euo pipefail
cd "$(dirname "$0")"

setenv() {  # grava CHAVE='valor' no .env (aspas simples: o compose não expande $ do hash bcrypt)
	grep -v "^$1=" .env >.env.tmp || true
	printf "%s='%s'\n" "$1" "$2" >>.env.tmp
	mv .env.tmp .env
	chmod 600 .env
}
ler_senha() {  # $1 = rótulo; repete até as duas digitações baterem
	local a b
	while true; do
		read -rsp "$1: " a; echo
		read -rsp "Repita: " b; echo
		[[ -n "$a" && "$a" == "$b" ]] && { REPLY="$a"; return; }
		echo "Não conferem ou vazia, de novo."
	done
}

atual=$(grep '^PAINEL_USUARIO=' .env | cut -d= -f2- | tr -d "'")
read -rp "Usuário do painel e dos avisos [${atual:-admin}]: " U
U=${U:-${atual:-admin}}

echo; echo "== 1/3 Painel (https://seu-dominio.exemplo.com)"
ler_senha "Senha do painel"
setenv PAINEL_USUARIO "$U"
setenv PAINEL_HASH "$(docker run --rm caddy:2.11.7 caddy hash-password --plaintext "$REPLY")"

echo; echo "== 2/3 Avisos (app ntfy, servidor https://seu-dominio.exemplo.com:8443)"
ler_senha "Senha do ntfy (pode ser a mesma do painel)"
if docker compose exec -T ntfy ntfy user list 2>/dev/null | grep -q "^user $U "; then
	docker compose exec -T -e NTFY_PASSWORD="$REPLY" ntfy ntfy user change-pass "$U" >/dev/null
else
	docker compose exec -T -e NTFY_PASSWORD="$REPLY" ntfy ntfy user add --role=admin "$U" >/dev/null
fi
tok=$(docker compose exec -T ntfy ntfy token add --label=servidor-casa "$U" | grep -o 'tk_[A-Za-z0-9]*' | head -1)
[[ -n "$tok" ]] || { echo "Não consegui criar o token do ntfy."; exit 1; }
setenv NTFY_TOKEN "$tok"

echo; echo "== 3/3 Chave da API da Claude (console.anthropic.com > API Keys). Enter vazio mantém a atual."
read -rsp "ANTHROPIC_API_KEY: " K; echo
[[ -n "$K" ]] && setenv ANTHROPIC_API_KEY "$K"

unset REPLY K
docker compose up -d >/dev/null 2>&1
echo; echo "Pronto. Usuário: $U. Painel e avisos reiniciados com a nova configuração."
