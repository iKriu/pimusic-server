# Образ для сборки Android (Capacitor): JDK 21. Node, Android SDK и Gradle — в смонтированном /cache.
# Базовый образ — по digest (Temurin 21.0.12.1+1, Ubuntu 24.04, linux/amd64); пакеты apt — из снимка
# snapshot.ubuntu.com на дату APT_SNAPSHOT. Обновление: новый digest (podman images --digests) + новая дата.
FROM docker.io/library/eclipse-temurin:21-jdk-noble@sha256:107215954359f1ab0be058a247d7c64ed6e69c71c1682d471d38ef751ec8e964
ARG APT_SNAPSHOT=20261018T000000Z
RUN sed -i -E "s#http://(archive|security)\.ubuntu\.com/ubuntu/?#http://snapshot.ubuntu.com/ubuntu/${APT_SNAPSHOT}/#" \
      /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends unzip xz-utils file \
    && rm -rf /var/lib/apt/lists/*
