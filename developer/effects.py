"""Do not repeat issue/comment creation after retries or an uncertain response."""
import hashlib
import json


def once(db,request_id,operation,arguments,execute):
    fingerprint=hashlib.sha256(json.dumps(arguments,sort_keys=True).encode()).hexdigest()
    key=(str(request_id),operation,fingerprint)
    row=db.execute('SELECT state,result FROM developer_effects WHERE request_id=? AND operation=? AND fingerprint=?',key).fetchone()
    if row:
        if row[0]=='completed':return json.loads(row[1])
        raise ValueError('The earlier request may already have succeeded. Search Jira before creating it again; it was not repeated automatically.')
    with db:
        db.execute("INSERT INTO developer_effects VALUES (?,?,?,'pending',NULL)",key)
    result=execute()
    with db:
        db.execute("UPDATE developer_effects SET state='completed',result=? WHERE request_id=? AND operation=? AND fingerprint=?",(json.dumps(result),*key))
    return result
