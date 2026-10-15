FROM lscr.io/linuxserver/wireguard:latest

# Пакеты ставятся поверх базового образа. Сам /init не меняем:
# контейнер остаётся под управлением s6, а VPN включает наше приложение.
RUN apk add --no-cache \
        python3 \
        py3-pip \
        py3-yaml \
        py3-feedparser \
        aria2 \
        iputils \
        bind-tools \
        curl \
        ca-certificates \
        traceroute \
    && pip3 install --no-cache-dir --break-system-packages \
        "aiogram>=3.20,<4" \
        "cryptography>=43" \
        "requests>=2.32" \
    && ln -sf "$(command -v python3)" /usr/local/bin/python

# Образ сам запускает всё из /config/wg_confs и при пустом каталоге
# удаляет маршрут по умолчанию. Нам это не нужно: туннель включает скрипт.
COPY worker/s6/svc-wireguard-run /etc/s6-overlay/s6-rc.d/svc-wireguard/run
COPY worker/s6/svc-potok-run /etc/s6-overlay/s6-rc.d/svc-potok/run
COPY worker/s6/svc-potok-type /etc/s6-overlay/s6-rc.d/svc-potok/type
RUN chmod 755 /etc/s6-overlay/s6-rc.d/svc-wireguard/run \
        /etc/s6-overlay/s6-rc.d/svc-potok/run \
    && mkdir -p /etc/s6-overlay/s6-rc.d/svc-potok/dependencies.d \
    && touch /etc/s6-overlay/s6-rc.d/svc-potok/dependencies.d/init-services \
    && touch /etc/s6-overlay/s6-rc.d/user/contents.d/svc-potok

WORKDIR /app
COPY src /app/src

ENV PYTHONPATH=/app/src \
    POTOK_CONFIG=/etc/potok/config.yaml
