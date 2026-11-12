"""Run the public API's complete local lead workflow using synthetic contacts."""
import argparse
import time
from uuid import uuid4
import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8000')
    args=parser.parse_args()
    with httpx.Client(base_url=args.base_url,timeout=20) as client:
        response=client.post('/api/v1/sessions',json={});response.raise_for_status()
        created=response.json()
        path='/api/v1/sessions/'+created['session']['id']
        client.headers['Authorization']='Bearer '+created['session_token']
        payload={'client_message_id':str(uuid4()),'text':'Свяжите меня с менеджером'}
        response=client.post(path+'/turns',json=payload);response.raise_for_status()
        deadline=time.monotonic()+30
        while response.status_code==202 and time.monotonic()<deadline:
            time.sleep(.1)
            response=client.get(path+'/turns/'+response.json()['turn_id']);response.raise_for_status()
        assert response.json()['status']=='completed'
        assert response.json()['decision']['kind']=='collecting_contacts'
        response=client.patch(path+'/lead-draft',json={'expected_revision':0,'fields':{
            'full_name':'Тестовый Клиент','phone':'+79990000000','company':'ООО Тест'}})
        response.raise_for_status()
        preview=client.post(path+'/lead-preview',json={});preview.raise_for_status()
        confirmation={'idempotency_key':str(uuid4()),'preview_id':preview.json()['preview_id'],'confirm':True}
        saved=client.post(path+'/leads',json=confirmation);saved.raise_for_status()
        replay=client.post(path+'/leads',json=confirmation);replay.raise_for_status()
        assert saved.json()['lead']['id']==replay.json()['lead']['id']
        assert saved.json()['lead']['status']=='complete'
        history=client.get(path+'/messages');history.raise_for_status()
        assert len(history.json()['items'])==3
        closed=client.post(path+'/close',json={});closed.raise_for_status()
        assert closed.json()['session']['status']=='closed'
        print('PASS: session → manager request → contacts → preview → confirmed lead → idempotent retry → history → close')


if __name__=='__main__':
    main()
