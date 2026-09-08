# Local password login

The Server login uses the existing `workspace_users` accounts. It does not call Google, Auth0, or another external identity provider.

Set these values in the server `.env` file for the initial account bootstrap:

```dotenv
ARTICLE_AGENT_LOGIN_USERNAME=admin
ARTICLE_AGENT_LOGIN_PASSWORD=<long-random-password>
ARTICLE_AGENT_LOGIN_SESSION_SECONDS=43200
```

The migration adds a nullable `workspace_users.password_hash` column. The first successful login can bootstrap the configured account from the environment; after that, the database hash is authoritative and the plaintext value is never returned by an API or written to logs. Once the bootstrap hash exists, the password environment value can be removed and database-only login remains available. Existing `workspace_users` rows, roles, project permissions, and disabled status are preserved. External-provider passwords are not migrated because they were never local passwords.

By default, the username is also used as the existing `workspace_users.user_id`. Set `ARTICLE_AGENT_LOGIN_ORGANIZATION_ID` and `ARTICLE_AGENT_LOGIN_USER_ID` when the display login name differs from that user ID or when the user ID exists in more than one organization.

`APP_USERNAME` and `APP_PASSWORD` are accepted as compatibility aliases for the first two values. OIDC/Auth0 configuration is no longer read by the Server, and the normal login page has no external-provider button.

An authenticated user can change their own password from 全局设置 → 账户资料. The change requires the current password, stores only a salted PBKDF2 hash, rotates the user's session version, and appends an audit event at `POST /api/account/password`.
