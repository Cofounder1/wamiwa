from app.api.routers import accounts, admin, auth, business, hiring, machine, promotions, social, uploads

ROUTERS = (
    auth.router,
    accounts.router,
    social.router,
    hiring.router,
    business.router,
    promotions.router,
    uploads.router,
    admin.router,
    machine.agent_router,
    machine.company_router,
    machine.webhook_router,
)
