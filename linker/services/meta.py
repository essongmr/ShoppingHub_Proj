import requests
from django.conf import settings
class MetaClient:
    base='https://graph.instagram.com'
    def __init__(self,token=None,account=None):
        if account is not None and token is None:
            from linker.services.instagram_oauth import credentials_for_account
            token = credentials_for_account(account)
        self.token=token or settings.META_ACCESS_TOKEN
    def public_reply(self,comment_id,message):
        return self._post(f'/{comment_id}/replies',{'message':message})
    def private_reply(self,comment_id,message):
        return self._post('/me/messages',{'recipient':{'comment_id':comment_id},'message':{'text':message}})
    def _post(self,path,payload):
        r=requests.post(self.base+path,params={'access_token':self.token},json=payload,timeout=15); r.raise_for_status(); return r.json()
