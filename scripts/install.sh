#!/bin/sh
# Установка Потока на Linux и macOS: Docker, примеры настроек, запуск сайта.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

docker_ready() {
  docker info >/dev/null 2>&1
}

wait_docker() {
  echo "Жду, пока Docker запустится..."
  i=0
  while [ "$i" -lt 90 ]; do
    if docker_ready; then
      return 0
    fi
    i=$((i + 1))
    sleep 4
  done
  echo "Docker так и не запустился. Перезагрузите компьютер и запустите скрипт ещё раз."
  return 1
}

wait_url() {
  url=$1
  i=0
  while [ "$i" -lt 80 ]; do
    code="$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 --max-time 8 "$url" || true)"
    if [ -n "$code" ] && [ "$code" != "000" ]; then
      return 0
    fi
    i=$((i + 1))
    sleep 3
  done
  echo "Не дождался ответа от $url"
  return 1
}

run_compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose up -d --build
  else
    docker-compose up -d --build
  fi
}

install_docker_linux() {
  echo "Docker не найден. Ставлю пакет docker из магазина системы."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y docker.io docker-compose-v2 || sudo apt-get install -y docker.io docker-compose
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y docker docker-compose
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y docker docker-compose
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm docker docker-compose
  else
    echo "Не знаю, как поставить Docker в этой системе. Поставьте Docker вручную и запустите скрипт ещё раз."
    exit 1
  fi
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl enable --now docker
  fi
}

install_docker_macos() {
  echo "Docker не найден. Ставлю Docker Desktop через Homebrew."
  if ! command -v brew >/dev/null 2>&1; then
    echo "Сначала поставьте Homebrew: https://brew.sh затем запустите скрипт ещё раз."
    exit 1
  fi
  brew install --cask docker
  open -a Docker
}

os="$(uname -s)"
if ! command -v docker >/dev/null 2>&1; then
  if [ "$os" = "Darwin" ]; then
    install_docker_macos
  else
    install_docker_linux
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker установлен, но команда docker ещё не видна. Перезагрузите компьютер и запустите скрипт ещё раз."
    exit 1
  fi
fi

if ! docker_ready; then
  if [ "$os" = "Darwin" ]; then
    open -a Docker 2>/dev/null || true
  fi
  wait_docker
fi

if [ ! -f "$ROOT/config/config.yaml" ]; then
  cp "$ROOT/config/config.yaml.example" "$ROOT/config/config.yaml"
  echo "Создан config/config.yaml из примера без личных ID ВКонтакте."
fi

if [ ! -f "$ROOT/config/wg0.conf" ]; then
  cp "$ROOT/config/wg0.conf.example" "$ROOT/config/wg0.conf"
  echo "Скопирован пример VPN. Без своих ключей WireGuard лента Рутрекера может не открыться. Сайт при этом уже работает."
fi

echo "Собираю и запускаю контейнеры..."
run_compose

echo "Жду сайт и Jackett..."
wait_url "http://localhost:8080/health"
wait_url "http://localhost:9117"

echo
echo "Сайт: http://localhost:8080"
echo "Вход: имя potok, пароль potok."
echo "Сразу смените пароль в мастере настройки (он откроется после входа)."

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://localhost:8080" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
  open "http://localhost:8080" || true
fi
