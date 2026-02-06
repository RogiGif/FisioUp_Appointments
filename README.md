# marcacoes

## Permissões e grupos

Depois de correr as migrações, executa:

```
python manage.py bootstrap_roles
```

Isto cria os grupos `ADMIN`, `RECEPTION`, `TECHNICIAN` e atribui as permissões necessárias ao backoffice e calendário global.
