# Yükleme Rehberi: `ugurtanyeriproje.com` (7/24, Otomatik HTTPS)

Bu klasör (`deploy/`) sunucuya konacak **tek pakettir**. İçinde:
- `web/` → üyelikli web sitesi (kayıt, giriş, proje yükleme, yönetim paneli)
- hesaplama motoru dosyaları + Excel şablonu (`Klima Hesap Tablosu_HAM_YEDEK.xlsm`)
- `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `.env.example` → `ugurtanyeriproje.com` ve `www.ugurtanyeriproje.com` için tek komutla çalıştırma + otomatik ücretsiz SSL/HTTPS (Let's Encrypt)

---

## 1. Porkbun DNS (A Kaydı) Ayarı

Porkbun yalnızca alan adını (`ugurtanyeriproje.com`) sağlar; alan adının yönleneceği **Sunucu IP adresi (A kaydı)** sitenin çalışacağı bulut sunucudan (VPS veya RepoCloud sunucunuzdan) alınır.

1. Sunucu panelinizden (RepoCloud / Hetzner / DigitalOcean / Contabo vb.) sunucunuzun **Public IPv4** adresini kopyalayın (ör. `123.45.67.89`).
2. **Porkbun** panelinde **Domain Management** → `ugurtanyeriproje.com` satırında **DNS** (Edit) bağlantısına tıklayın.
3. Varsayılan park kayıtları (varsa `ALIAS` / `CNAME` `pixie.porkbun.com`) silin ve şu **2 adet `A` kaydını** ekleyin:

| Type | Host | Answer (Value) | TTL |
|---|---|---|---|
| **A** | *(boş bırakın)* | `SUNUCU_IPV4_ADRESINIZ` | `600` |
| **A** | `www` | `SUNUCU_IPV4_ADRESINIZ` | `600` |

---

## 2. Sunucuda Kurulum Adımları (Ubuntu 24.04 — 1–2 GB RAM)

1. **Sunucuya bağlanın:**
   ```bash
   ssh root@SUNUCU_IPV4_ADRESINIZ
   ```
2. **Docker kurun (kurulu değilse):**
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
3. **Paketi kendi bilgisayarınızdan sunucuya yükleyin** (`Projeler\çalışma` klasöründe PowerShell açıp):
   ```powershell
   scp -r deploy root@SUNUCU_IPV4_ADRESINIZ:/root/
   ```
4. **Sunucuda `.env` dosyasını oluşturun:**
   ```bash
   cd /root/deploy
   cp .env.example .env
   nano .env
   ```
   İçeriği:
   ```env
   ALAN_ADI=ugurtanyeriproje.com
   ADMIN_EPOSTA=admin@ugurtanyeriproje.com
   ADMIN_SIFRE=Buraya-En-Az-12-Karakterli-Guclu-Bir-Sifre-Yazin
   GUVENLI_CEREZ=1
   MAX_YUKLEME_MB=300
   SAKLAMA_GUN=7
   ```
5. **Servisi başlatın:**
   ```bash
   docker compose up -d --build
   ```
6. Tarayıcıda **`https://ugurtanyeriproje.com`** adresini açın. Caddy birkaç saniye içinde otomatik HTTPS sertifikasını alır.

---

## 3. Günlük Kullanım
- **Yönetim Paneli:** `https://ugurtanyeriproje.com/admin`
- **Yeni üye:** Müşteri `/kayit` sayfasından kayıt olur → `/admin` panelinde paket seçip **"Varsayılanları uygula"** kutusunu işaretleyerek **Kaydet**'e basın.
- **Paketler:** Deneme (1 hak, 7 gün), Başlangıç (15 hak, 30 gün), Ofis (40 hak, 30 gün).
- **Hak düşümü:** Yalnızca başarılı Excel (`.xlsm`) üretiminde 1 hak düşer.
- **Yedek alma:**
  ```bash
  docker run --rm -v deploy_veri:/v -v $PWD:/y alpine tar czf /y/yedek.tgz -C /v .
  ```

## 4. Yerelde Denemek (Kendi Bilgisayarınızda)
`BASLAT_WEB_YEREL.bat` dosyasına çift tıklayın → tarayıcıda `http://127.0.0.1:8080`.
Yönetici girişi: `admin@yerel.test` / `Degistir-Bu-Sifreyi-1`
