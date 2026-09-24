# Porting manifest

## COPY NOW
- Django config / Docker / requirements
- linker models
- migrations 0001~0016
- services: account_onboarding, instagram_oauth, meta, rules, shoppinghub_dm, youtube_*
- tasks
- creator/workspace views (backend behavior retained)
- reset_creator_test_data management command
- `.env.example`

## REBUILT FRESH
- `linker/urls.py`: canonical routes only; legacy unprefixed routes and UI preview removed
- production templates: minimal functional UI generated from scratch

## DEFER UNTIL STABLE
- prior Creator/onboarding visual design
- `docs/shoppinghub_ui_v1/*`
- UI preview screens
- placeholder screens
- prior public store/product presentation styling
- prior workspace visual styling

## EXCLUDE
- `.env`
- media/uploads
- `__pycache__`, `.pyc`
- nested source ZIP
- old UI preview implementation
- legacy unprefixed URLs (`/accounts/`, `/contents/`, `/rules/`...)

## META/INSTAGRAM
Existing Meta app settings can be reused because callback paths are preserved. Secrets must be copied manually into the new `.env`. If the public hostname stays the same and nginx points it to the new container, Meta Console redirect URI does not need to change.
