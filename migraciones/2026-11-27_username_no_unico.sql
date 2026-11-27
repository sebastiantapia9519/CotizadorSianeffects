-- El nombre de usuario se puede repetir; solo el correo debe ser único.
-- Correr una vez en producción (en dev ya no existe la restricción).
-- Es idempotente: si la restricción no existe, no hace nada.

ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_username_key;

-- Verificación: debe listar solo usuarios_email_key y usuarios_pkey
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'usuarios'::regclass AND contype IN ('u', 'p');
