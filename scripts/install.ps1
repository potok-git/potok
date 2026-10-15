# Установка Потока на Windows: Docker, примеры настроек, запуск сайта.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

function Test-DockerReady {
    docker info 1>$null 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Install-DockerDesktop {
    Write-Host "Docker не найден. Ставлю Docker Desktop. Может появиться окно согласия Windows."
    winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось поставить Docker Desktop. Поставьте его вручную и запустите этот скрипт ещё раз."
    }
}

function Start-DockerDesktop {
    $exe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path -LiteralPath $exe) {
        Start-Process -FilePath $exe
        return
    }
    $cmd = Get-Command "Docker Desktop.exe" -ErrorAction SilentlyContinue
    if ($cmd) {
        Start-Process -FilePath $cmd.Source
    }
}

function Wait-Docker {
    Write-Host "Жду, пока Docker запустится..."
    $deadline = (Get-Date).AddMinutes(6)
    while ((Get-Date) -lt $deadline) {
        if (Test-DockerReady) {
            return
        }
        Start-Sleep -Seconds 4
    }
    throw "Docker так и не запустился. Если Windows просит перезагрузку - перезагрузите компьютер и запустите скрипт ещё раз."
}

function Wait-Url([string]$Url) {
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline) {
        try {
            $code = & curl.exe -s -o NUL -w "%{http_code}" --connect-timeout 3 --max-time 8 $Url
            if ($code -and $code -ne "000") {
                return
            }
        } catch {
        }
        Start-Sleep -Seconds 3
    }
    throw "Не дождался ответа от $Url"
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    try {
        Install-DockerDesktop
    } catch {
        Write-Host "Не получилось поставить Docker. Если система просит перезагрузку - перезагрузите компьютер и запустите скрипт ещё раз."
        throw
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Host "Docker установлен, но команда docker ещё не видна. Перезагрузите компьютер и запустите скрипт ещё раз."
        exit 1
    }
}

if (-not (Test-DockerReady)) {
    Start-DockerDesktop
    try {
        Wait-Docker
    } catch {
        Write-Host "Перезагрузите компьютер и запустите скрипт ещё раз."
        throw
    }
}

$ConfigYaml = Join-Path $Root "config\config.yaml"
$ConfigExample = Join-Path $Root "config\config.yaml.example"
if (-not (Test-Path -LiteralPath $ConfigYaml)) {
    if (-not (Test-Path -LiteralPath $ConfigExample)) {
        throw "Нет файла config/config.yaml.example. Скачайте проект целиком."
    }
    Copy-Item -LiteralPath $ConfigExample -Destination $ConfigYaml
    Write-Host "Создан config/config.yaml из примера без личных ID ВКонтакте."
}

$Wg = Join-Path $Root "config\wg0.conf"
$WgExample = Join-Path $Root "config\wg0.conf.example"
if (-not (Test-Path -LiteralPath $Wg)) {
    Copy-Item -LiteralPath $WgExample -Destination $Wg
    Write-Host "Скопирован пример VPN. Без своих ключей WireGuard лента Рутрекера может не открыться. Сайт при этом уже работает."
}

Write-Host "Собираю и запускаю контейнеры..."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "docker compose не запустился. Проверьте, что Docker Desktop открыт."
}

Write-Host "Жду сайт и Jackett..."
Wait-Url "http://localhost:8080/health"
Wait-Url "http://localhost:9117"

Write-Host ""
Write-Host "Сайт: http://localhost:8080"
Write-Host "Вход: имя potok, пароль potok."
Write-Host "Сразу смените пароль в мастере настройки (он откроется после входа)."
Start-Process "http://localhost:8080"
