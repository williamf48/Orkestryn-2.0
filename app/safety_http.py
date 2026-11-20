"""Small Safety Lab API adapter using the existing account and CSRF boundary."""
from urllib.parse import parse_qs,urlsplit
import safety_environment as env
import safety_lab as research
import safety_live
import safety_ui
import oversight_store
import named_access


def query(handler,field):
    value=parse_qs(urlsplit(handler.path).query,keep_blank_values=True,max_num_fields=1)
    if set(value)!={field} or len(value[field])!=1 or not value[field][0]:
        raise ValueError('Choose one exact research '+field+'.')
    return value[field][0]


def get(handler,path,auth):
    if path in ('/api/oversight/round-001','/api/oversight/return-bounds','/api/oversight/round-002',
                '/api/oversight/round-002-summary','/api/oversight/label-case','/api/oversight/label-catalog','/api/oversight/paired-returns',
                '/api/oversight/round-003','/api/oversight/round-003-summary','/api/oversight/shadow-case','/api/oversight/joint-review',
                '/api/oversight/round-004','/api/oversight/round-004-summary','/api/oversight/checkpoint-case'):
        if '?' in handler.path and not path.endswith(('/label-case','/shadow-case','/checkpoint-case')):raise ValueError('This research route takes no query.')
        principal,_=handler.auth(auth)
        try:auth.require(principal,'manager')
        except named_access.AccessDenied:
            handler.reply(403,{'error':'A manager account is required to review these studies.'});return True
        import oversight_round as rounds
        if path.endswith('round-001'):
            raw,_=rounds.archive();handler.reply_archive(raw,'Orkestryn-round-001-evidence.zip')
        elif path.endswith(('round-004','round-004-summary')):
            import oversight_round4 as r4
            raw,s=r4.archive()
            if path.endswith('summary'):handler.reply(200,{key:s[key] for key in ('round','observations','localization','approval','handoff','scope')})
            else:handler.reply_archive(raw,'Orkestryn-round-004-evidence.zip')
        elif path.endswith(('round-003','round-003-summary')):
            import oversight_round3 as r3
            raw,s=r3.archive()
            if path.endswith('summary'):handler.reply(200,{'inspection':s['inspection'],'handoff':s['handoff'],'human':s['human'],'observations':s['observations']})
            else:handler.reply_archive(raw,'Orkestryn-round-003-evidence.zip')
        elif path.endswith(('round-002','round-002-summary')):
            import oversight_round2 as r2
            raw,s=r2.archive()
            if path.endswith('summary'):
                handler.reply(200,{'scope':s['source_type'],'handoff':s['handoff'],'budget':s['budget'],
                                   'legacy_transfer':s['legacy']['locked_transfer'],'new_domain':s['new_domain']})
            else:handler.reply_archive(raw,'Orkestryn-round-002-evidence.zip')
        else:
            with research.Lab(handler.server.root) as lab:
                oversight_store.setup(lab)
                if path.endswith('/label-catalog'):
                    saved=[{'id':r[0],'source':'Saved'} for r in lab.db.execute('SELECT id FROM candidates ORDER BY rowid DESC LIMIT 1000')]
                    fixtures=[{'id':c['id'],'source':c['private']['family']+' fixture'} for c in research.dataset()['cases']]
                    handler.reply(200,{'cases':saved+fixtures})
                elif path.endswith('/checkpoint-case'):
                    import oversight_receipts as receipts
                    fields=parse_qs(urlsplit(handler.path).query,keep_blank_values=True,max_num_fields=3)
                    if (set(fields)!={'id','stride','feed'} or any(len(v)!=1 for v in fields.values())
                            or fields['stride'][0] not in {'1','2','4','8'} or fields['feed'][0] not in {'complete','drop_final'}):
                        raise ValueError('Choose a case, checkpoint spacing, and receipt availability.')
                    c=lab.case(fields['id'][0]);v=env.public_context(c)
                    _,history,_=env.execute(v['initial_state'],v['untrusted_candidate']['actions'])
                    feed=receipts.checkpoint_feed(v['initial_state'],v['permissions'],history,int(fields['stride'][0]),fields['feed'][0]=='drop_final')
                    handler.reply(200,{'case':c['id'],'checkpoints':feed,'steps':len(history),
                        'findings':{rule:receipts.locate(feed,len(history),rule) for rule in ('snapshot_point','latched_interval')}})
                elif path.endswith('/shadow-case'):
                    import oversight_shadow as shadow
                    fields=parse_qs(urlsplit(handler.path).query,keep_blank_values=True,max_num_fields=3)
                    if set(fields)!={'id','budget','price'} or any(len(v)!=1 for v in fields.values()):
                        raise ValueError('Choose one case, one budget and one header price.')
                    if fields['budget'][0] not in {str(x) for x in range(1,17)} or fields['price'][0] not in {'0','1','4'}:
                        raise ValueError('Choose an offered cost and budget.')
                    c=lab.case(fields['id'][0]);view=env.public_context(c)
                    handler.reply(200,{'case':c['id'],'comparisons':{rule:shadow.audit(view,rule,int(fields['budget'][0]),int(fields['price'][0]))
                        for rule in ('prefix_allow','prefix_defer','indexed_defer')},'scope':'Read-only simulated inspection; no plan is run on a service.'})
                elif path.endswith('/joint-review'):
                    import oversight_pairs,oversight_shadow
                    handler.reply(200,oversight_shadow.gate(oversight_pairs.workspace(lab.db)))
                elif path.endswith('/label-case'):
                    import oversight_round2 as r2
                    c=lab.case(query(handler,'id'));ref=r2.legacy_labels(c)
                    guard=env.monitor(env.public_context(c),'full_guard',16)
                    handler.reply(200,{'case':c['id'],'source':(c.get('origin') or {}).get('kind','SCRIPTED_FIXTURE'),
                                       'reference':ref,'full_guard':guard,'legitimate_completion_available':ref['either'] is None and ref['task_complete'],
                                       'legitimate_completion_delivered':ref['either'] is None and ref['task_complete'] and guard['decision']=='allow'})
                elif path.endswith('/paired-returns'):
                    import oversight_pairs
                    handler.reply(200,oversight_pairs.workspace(lab.db))
                else:handler.reply(200,rounds.return_bounds(oversight_store.people.report(lab.db)['arms']))
        return True
    if oversight_store.get(handler,path,auth):return True
    if path=='/safety':
        handler.reply(200,safety_ui.PAGE,html=True);return True
    if path not in {'/api/safety','/api/safety/case','/api/safety/report','/safety/report','/api/safety/evidence'}:
        return False
    principal,csrf=handler.auth(auth)
    with research.Lab(handler.server.root) as lab:
        if path=='/api/safety':
            if urlsplit(handler.path).query:raise ValueError('The research overview has no query options.')
            handler.reply(200,{'principal':principal,'csrf':csrf,'model_requests_enabled':handler.server.allow_browser_keys,**lab.state()})
        elif path=='/api/safety/case':
            case=lab.case(query(handler,'id'))
            handler.reply(200,{'id':case['id'],'public_context':env.public_context(case),'view_digest':env.digest(env.public_context(case)),
                'scope':'This monitor projection excludes evaluator labels, private attack goals and oracle results.'})
        else:
            run=lab.run(query(handler,'run'))
            if path=='/api/safety/report':handler.reply(200,run)
            elif path=='/safety/report':handler.reply(200,research.render(run),html=True)
            else:handler.reply_archive(*research.export_bytes(run))
    return True


def post(handler,endpoint,body,principal,auth):
    if oversight_store.dispatch(handler,endpoint,body,principal,auth):return True
    if endpoint!='/api/safety':return False
    auth.require(principal,'manager')
    action=body.get('action')
    allowed={
        'evaluate':({'action'},{'split','budgets','case_id','framing'}),
        'import-candidate':({'action','base_id','candidate'},{'model'}),
        'paid-review':({'action','case_id','kind','output_cap'},{'visibility'}),
        'paid-attempt':({'action','case_id','kind','output_cap','review_digest','confirmed','nebius_api_key'},{'visibility'}),
    }
    if action not in allowed:raise ValueError('Choose a registered research operation.')
    required,optional=allowed[action]
    if not required.issubset(body) or set(body)-required-optional:raise ValueError('Use the exact research operation fields.')
    with research.Lab(handler.server.root) as lab:
        if action=='evaluate':
            value=lab.evaluate(split=body.get('split','development'),budgets=body.get('budgets',research.DEFAULT_BUDGETS),
                               case_id=body.get('case_id'),principal=principal,framing=body.get('framing',False))
        elif action=='import-candidate':
            case=lab.import_candidate(body['base_id'],body['candidate'],body.get('model','not reported'))
            value={'status':'SAVED','case_id':case['id'],'model_requests':0,'source':'IMPORTED_UNVERIFIED'}
        elif action=='paid-review':
            if not handler.server.allow_browser_keys:raise ValueError('This workspace disables entered-key model experiments.')
            value=safety_live.preview(lab,body['case_id'],body['kind'],body['output_cap'],body.get('visibility','full_context'))
        else:
            if not handler.server.allow_browser_keys:raise ValueError('This workspace disables entered-key model experiments.')
            value=safety_live.attempt(lab,body['case_id'],body['kind'],body['output_cap'],body['review_digest'],
                                     body['nebius_api_key'],confirmed=body['confirmed'],principal=principal,visibility=body.get('visibility','full_context'))
        handler.reply(200,value)
    return True
