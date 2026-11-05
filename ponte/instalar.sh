#!/data/data/com.termux/files/usr/bin/sh
# Instala a ponte numa casa sem PC, num celular Android com Termux (baixado pelo site, não pela Play Store).
# O site (Admin > Pontes > Configurar celular) monta o comando já com o código de pareamento:
#   apt update && yes | apt full-upgrade && curl -fsSL https://seu-dominio.exemplo.com/ponte/instalar.sh | CODIGO=123456 sh
# O curl já vem no Termux. Usar apt (o pkg usa o curl para escolher o espelho). Não usar "pkg install curl" antes do upgrade: atualiza só o curl, deixa o openssl
# velho e o curl quebra ("cannot locate symbol SSL_set_quic_tls_early_data_enabled", visto em 30/10).
# Sem CODIGO, o pareamento pergunta (python ponte.py parear).
set -e
VPS="${VPS:-https://seu-dominio.exemplo.com}"
DIR="$HOME/ponte"
export DEBIAN_FRONTEND=noninteractive
APT="apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

echo "== 1/4 atualizando o Termux e instalando o Python (alguns minutos na primeira vez)"
$APT update
$APT full-upgrade
$APT install python curl ca-certificates
$APT install termux-api || echo "   (termux-api não instalou: só não aparece a bateria no painel)"

echo "== 2/4 baixando a ponte de $VPS/ponte/"
mkdir -p "$DIR"
cd "$DIR"
case "$(uname -m)" in
  aarch64|arm64) BIN=go2rtc_arm64 ;;
  armv7l|armv8l|arm) BIN=go2rtc_armv7 ;;
  *) echo "processador $(uname -m) não suportado"; exit 1 ;;
esac
# baixa numa pasta à parte e troca com mv: com a ponte rodando, sobrescrever o go2rtc em uso dá "text file busy"
# (curl: (23) client returned ERROR on write, visto em 30/10); o mv troca o arquivo mesmo em uso
rm -rf novo && mkdir novo
curl -fsSLo novo/ponte.py "$VPS/ponte/ponte.py"
curl -fsSLo "novo/$BIN" "$VPS/ponte/$BIN"
curl -fsSLo novo/SHA256SUMS "$VPS/ponte/SHA256SUMS"
(cd novo && grep -E " ($BIN|ponte.py)\$" SHA256SUMS | sha256sum -c -)
chmod +x "novo/$BIN"
mv -f novo/ponte.py novo/"$BIN" novo/SHA256SUMS .
rmdir novo

echo "== 3/4 pareando com o servidor"
if [ -n "$CODIGO" ]; then
  PONTE_VPS="$VPS" PONTE_CODIGO="$CODIGO" PONTE_NOME="${NOME:-celular da ponte ($(getprop ro.product.model 2>/dev/null || echo Android))}" python ponte.py parear
elif [ ! -f config.json ]; then
  python ponte.py parear </dev/tty
fi

echo "== 4/4 deixando ligado (agora e quando o celular reiniciar)"
# iniciar.sh sobe o laco.sh (supervisor: religa a ponte se ela sair e aplica as atualizações automáticas) se ele
# não estiver rodando. Chamado pelo Termux:Boot, ao abrir o Termux e aqui. Os dois scripts vêm da ponte.py.
python ponte.py scripts
mkdir -p "$HOME/.termux/boot"
printf '#!/data/data/com.termux/files/usr/bin/sh\nsh %s/iniciar.sh\n' "$DIR" > "$HOME/.termux/boot/ponte.sh"
chmod +x "$HOME/.termux/boot/ponte.sh"
touch "$HOME/.bashrc"
grep -q "ponte/iniciar.sh" "$HOME/.bashrc" || echo "sh $DIR/iniciar.sh  # religa a ponte se o celular reiniciou" >> "$HOME/.bashrc"
pkill -f "ponte/laco.sh" 2>/dev/null || true
pkill -f "python ponte.py" 2>/dev/null || true
pkill -f "$BIN" 2>/dev/null || true
rm -f atualizando atualizacao_falhou versao_recusada
sleep 1
sh "$DIR/iniciar.sh"

echo
echo "Pronto: a ponte está rodando."
if grep -q "^xiaomi:" go2rtc.yaml 2>/dev/null; then
  echo "Pode sair do Termux pelo botão de voltar (sem tocar em Exit na notificação)."
else
  echo "Volte para a página do site no navegador: o próximo passo é entrar na conta Xiaomi (botão do passo 3)."
fi
echo "Se o celular reiniciar: a ponte volta sozinha com o app Termux:Boot (abra-o uma vez) ou ao abrir o Termux."
echo "Daqui em diante a ponte se atualiza sozinha (o painel mostra a versão)."
