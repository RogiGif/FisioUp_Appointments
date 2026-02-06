# Deploy — marcacoes

Este documento prepara o deploy em produção **sem alterar fluxos/UX**.

## 1) Variáveis de ambiente

Obrigatórias:
- `ENV=production`
- `SECRET_KEY=...`
- `ALLOWED_HOSTS=dominio.com,www.dominio.com`

Base de dados (MySQL):
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_HOST`
- `DB_PORT`

Email (SMTP):
- `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`
- `EMAIL_HOST=...`
- `EMAIL_PORT=587`
- `EMAIL_HOST_USER=...`
- `EMAIL_HOST_PASSWORD=...`
- `EMAIL_USE_TLS=1`
- `DEFAULT_FROM_EMAIL=...`

Opcional (segurança SSL/HSTS):
- `SECURE_SSL_REDIRECT=1`
- `SECURE_HSTS_SECONDS=0` (aumentar depois do HTTPS validado)
- `SECURE_HSTS_INCLUDE_SUBDOMAINS=0|1`
- `SECURE_HSTS_PRELOAD=0|1`

## 2) Passos de deploy

1. Instalar dependências
   - `pip install -r requirements.txt`

2. Migrar base de dados
   - `python manage.py migrate`

3. Recolher estáticos
   - `python manage.py collectstatic`

4. Iniciar servidor
   - `ENV=production DEBUG=0 python manage.py runserver 0.0.0.0:8000` (dev/prod simples)

## 3) Static & Media

- `STATIC_ROOT` está configurado para `staticfiles/`.
- `MEDIA_ROOT` está configurado para `media/`.
- Em produção, servir `staticfiles/` e `media/` via Nginx ou outro servidor web.
- Em desenvolvimento, o Django serve media automaticamente.

## 4) Healthcheck

- Endpoint: `/health/`
- Resposta: `{ "status": "ok", "db": true }`

## 5) Email em produção

- Garante SPF/DKIM/DMARC configurados no domínio:
  - SPF: autoriza o servidor SMTP a enviar em nome do domínio.
  - DKIM: assina mensagens para reduzir spam.
  - DMARC: política de alinhamento SPF/DKIM.

## 6) Checklist pós‑deploy (smoke test)

- [ ] Abrir `/health/`
- [ ] Login com utilizador staff
- [ ] Abrir `/backoffice/`
- [ ] Criar marcação no backoffice
- [ ] Ver `/prof/calendario/`
- [ ] Enviar password reset

## 7) Backups

- Fazer backup diário da base de dados.
- Guardar ficheiros de `media/` (fotos) separadamente.
