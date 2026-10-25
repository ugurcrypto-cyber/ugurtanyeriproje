FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 VERI_DIZINI=/veri GUVENLI_CEREZ=1 ADMIN_EPOSTA=ugur.tanyeri@hotmail.com ADMIN_SIFRE=Degistir-Bu-Sifreyi-1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME /veri
EXPOSE 8080
# Tek isci: agir isler sirayla calisir (bellek korumasi) ve SQLite ile uyumlu. Is parcacigi sayisi esanli kullanici icindir.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "600", "-b", "0.0.0.0:8080", "web.app:app"]
