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

Media (uploads):
- `MEDIA_ROOT=/caminho/absoluto/para/media`
- opcional: `MEDIA_URL=/media/`

Sessão / segurança:
- `INTERNAL_SESSION_TIMEOUT_SECONDS=14400`
- `CLIENT_SESSION_TIMEOUT_SECONDS=3600`
- `INTERNAL_SESSION_WARNING_SECONDS=600`
- `CLIENT_SESSION_WARNING_SECONDS=300`
- `SESSION_KEEPALIVE_INTERVAL_SECONDS=300`

Moloni API:
- `MOLONI_CLIENT_ID=...`
- `MOLONI_CLIENT_SECRET=...`
- opcional: `MOLONI_COMPANY_ID=...` (só se houver várias empresas e quiseres forçar uma)
- `MOLONI_BASE_URL=https://api.moloni.pt/v1`
- `MOLONI_REDIRECT_URI=https://marcacoes.fisio-up.pt/backoffice/settings/moloni/callback/`

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
   python manage.py collectstatic --noinput

   - Sempre que houver alterações no site público (`website/static/website/...`), repetir este passo.

4. Reiniciar a app Python
   - necessário quando muda o `.env` (ex: timeouts de sessão)

5. Iniciar servidor
   - `ENV=production DEBUG=0 python manage.py runserver 0.0.0.0:8000` (dev/prod simples)

## 3) Static & Media

- `STATIC_ROOT` está configurado para `staticfiles/`.
- `MEDIA_ROOT` por omissão é `media/`, mas em produção deve ser definido via env (ex: `/var/www/fisioapp/media`).
- Em produção, servir `staticfiles/` e `media/` via Nginx ou outro servidor web.
- Em desenvolvimento, o Django serve media automaticamente.

Exemplo Nginx:
```nginx
location /media/ {
    alias /var/www/fisioapp/media/;
    expires 30d;
    add_header Cache-Control "public";
}

location /static/ {
    alias /var/www/fisioapp/staticfiles/;
    expires 30d;
    add_header Cache-Control "public";
}
```

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
- [ ] Confirmar aviso de sessão e logout automático por inatividade
- [ ] Ligar Moloni em `/backoffice/settings/moloni/`
- [ ] Testar ligação Moloni
- [ ] Sincronizar clientes Moloni

## 7) Backups

- Fazer backup diário da base de dados.
- Guardar ficheiros de `media/` (fotos) separadamente.
