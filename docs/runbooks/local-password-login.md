# Local password login

The Server login uses one deployment account and the existing workspace Actor. It does not call Google, Auth0, or another external identity provider.

Set these values in the server `.env` file before starting the backend:

```dotenv
ARTICLE_AGENT_LOGIN_USERNAME=admin
ARTICLE_AGENT_LOGIN_PASSWORD=<long-random-password>
ARTICLE_AGENT_LOGIN_SESSION_SECONDS=43200
```

`ARTICLE_AGENT_LOGIN_PASSWORD` is read only from the process environment and is never stored in PostgreSQL, returned by an API, or written to logs. The configured organization and user must be an active row in `organizations` and `workspace_users`; their existing project permissions remain authoritative.

By default, the username is also used as the existing `workspace_users.user_id`. Set `ARTICLE_AGENT_LOGIN_ORGANIZATION_ID` and `ARTICLE_AGENT_LOGIN_USER_ID` when the display login name differs from that user ID or when the user ID exists in more than one organization.

`APP_USERNAME` and `APP_PASSWORD` are accepted as compatibility aliases for the first two values. OIDC is disabled unless `ARTICLE_AGENT_ENABLE_OIDC=true` is explicitly set, and the normal login page never displays an external-provider button.
