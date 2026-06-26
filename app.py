import sqlite3, hashlib, jwt, json, os, math, secrets
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)
SECRET = 'hibee-secret-key-2024-affiliates'
DB_PATH = 'hibee.db'
DATABASE_URL = os.environ.get('DATABASE_URL')
IS_PG = bool(DATABASE_URL)

MONTHS = ['janv','fevr','mars','avr','mai','juin','juil','aout','sept','oct','nov','dec']
MONTH_LABELS = ['Janvier','Février','Mars','Avril','Mai','Juin','Juillet','Août','Septembre','Octobre','Novembre','Décembre']

# ── DB ABSTRACTION ────────────────────────────────────────────────────────────

class Row(dict):
    """Dict accessible par clé comme sqlite3.Row"""
    def __getitem__(self, key):
        return super().__getitem__(key)
    def __getattr__(self, key):
        try: return self[key]
        except KeyError: raise AttributeError(key)

class Cursor:
    def __init__(self, cur, is_pg=False):
        self._cur = cur
        self._is_pg = is_pg
        self.lastrowid = None

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None: return None
        return Row(row) if self._is_pg else Row(dict(row))

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows: return []
        return [Row(r) if self._is_pg else Row(dict(r)) for r in rows]

class Conn:
    """Connexion unifiée SQLite / PostgreSQL"""
    def __init__(self):
        self._pg_conn = None
        self._sq_conn = None
        if IS_PG:
            import psycopg2, psycopg2.extras
            self._pg_conn = psycopg2.connect(DATABASE_URL + '?sslmode=require'
                                              if '?' not in DATABASE_URL else DATABASE_URL)
        else:
            self._sq_conn = sqlite3.connect(DB_PATH)
            self._sq_conn.row_factory = sqlite3.Row
            self._sq_conn.execute("PRAGMA foreign_keys = ON")
        self._last_cur = None

    def execute(self, sql, params=()):
        if IS_PG:
            import psycopg2.extras
            sql = sql.replace('?', '%s')
            cur = self._pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            self._last_cur = cur
            c = Cursor(cur, True)
        else:
            raw = self._sq_conn.execute(sql, params)
            self._last_cur = raw
            c = Cursor(raw, False)
        return c

    @property
    def lastrowid(self):
        if IS_PG:
            try:
                cur = self._pg_conn.cursor()
                cur.execute("SELECT lastval()")
                return cur.fetchone()[0]
            except: return None
        return self._last_cur.lastrowid if self._last_cur else None

    def commit(self):
        (self._pg_conn or self._sq_conn).commit()

    def close(self):
        (self._pg_conn or self._sq_conn).close()

def get_db():
    return Conn()

# ── SQL helpers ───────────────────────────────────────────────────────────────

def AUTO_ID():
    return 'SERIAL PRIMARY KEY' if IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'

def IGNORE(table, cols, vals_ph, conflict_col):
    if IS_PG:
        return f"INSERT INTO {table} ({cols}) VALUES ({vals_ph}) ON CONFLICT ({conflict_col}) DO NOTHING"
    return f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({vals_ph})"

def UPSERT(table, cols, vals_ph, conflict_col, update_pairs):
    if IS_PG:
        return f"INSERT INTO {table} ({cols}) VALUES ({vals_ph}) ON CONFLICT ({conflict_col}) DO UPDATE SET {update_pairs}"
    return f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({vals_ph})"

def col_exists(conn, table, col):
    if IS_PG:
        r = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name=? AND column_name=?", (table, col)).fetchone()
        return r is not None
    else:
        cols = [r['name'] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        return col in cols

# ── INIT DB ───────────────────────────────────────────────────────────────────

def init_db():
    conn = get_db()
    A = AUTO_ID()
    conn.execute(f'''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
    conn.execute(IGNORE('settings','key,value','?,?','key'), ('prix_par_employe','200'))
    conn.execute(IGNORE('settings','key,value','?,?','key'), ('annee_suivi','2026'))

    conn.execute(f'''CREATE TABLE IF NOT EXISTS users (
        id {A}, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'affiliate')''')

    conn.execute(f'''CREATE TABLE IF NOT EXISTS affiliates (
        id {A}, user_id INTEGER REFERENCES users(id),
        nom TEXT, prenom TEXT, email TEXT, telephone TEXT, instagram TEXT,
        taux_commission REAL DEFAULT 0.3, duree_commission INTEGER DEFAULT 3,
        date_adhesion TEXT, statut TEXT DEFAULT 'Actif',
        code_parrain TEXT UNIQUE, token_public TEXT UNIQUE)''')

    conn.execute(f'''CREATE TABLE IF NOT EXISTS salons (
        id {A}, affiliate_id INTEGER REFERENCES affiliates(id) ON DELETE CASCADE,
        nom TEXT, ville TEXT, nb_employes INTEGER DEFAULT 0, date_debut TEXT)''')

    monthly_cols = ' '.join([f', comm_{m} REAL DEFAULT 0, paid_{m} INTEGER DEFAULT 0' for m in MONTHS])
    conn.execute(f'''CREATE TABLE IF NOT EXISTS commissions (
        id {A}, salon_id INTEGER UNIQUE REFERENCES salons(id) ON DELETE CASCADE,
        annee INTEGER DEFAULT 2026 {monthly_cols})''')

    conn.execute(f'''CREATE TABLE IF NOT EXISTS versements (
        id {A}, affiliate_id INTEGER REFERENCES affiliates(id) ON DELETE CASCADE,
        montant REAL, date TEXT, note TEXT)''')

    # Migration token_public
    if not col_exists(conn, 'affiliates', 'token_public'):
        conn.execute("ALTER TABLE affiliates ADD COLUMN token_public TEXT")
        conn.commit()

    # Générer tokens manquants
    for a in conn.execute("SELECT id FROM affiliates WHERE token_public IS NULL").fetchall():
        conn.execute("UPDATE affiliates SET token_public=? WHERE id=?", (secrets.token_urlsafe(24), a['id']))

    # Admin
    conn.execute(IGNORE('users','email,password,role','?,?,?','email'),
                 ('admin@hibee.ma', hash_pw('HibeeSAAS2026!'), 'admin'))
    conn.execute("UPDATE users SET password=? WHERE email=? AND role='admin'",
                 (hash_pw('HibeeSAAS2026!'), 'admin@hibee.ma'))

    # Affilié démo
    conn.execute(IGNORE('users','email,password,role','?,?,?','email'),
                 ('affilie1@hibee.ma', hash_pw('Affilie2024!'), 'affiliate'))
    conn.commit()

    user = conn.execute("SELECT id FROM users WHERE email='affilie1@hibee.ma'").fetchone()
    if user:
        existing = conn.execute("SELECT id FROM affiliates WHERE user_id=?", (user['id'],)).fetchone()
        if not existing:
            tok = secrets.token_urlsafe(24)
            conn.execute('''INSERT INTO affiliates
                (user_id,nom,prenom,email,telephone,instagram,taux_commission,
                 duree_commission,date_adhesion,statut,code_parrain,token_public)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (user['id'],'Ben Youssef','Sarbine','affilie1@hibee.ma',
                 '+212 6XX XXX XXX','@influenceur1',0.3,3,'2026-01-01','Actif','AFF001',tok))
            conn.commit()
    conn.close()

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else None

def calc_commissions(nb_employes, date_debut, taux, duree, prix):
    abo = nb_employes * prix
    comm_vals = {m: 0.0 for m in MONTHS}
    if date_debut and abo > 0 and duree > 0:
        try:
            dt = datetime.strptime(date_debut, '%Y-%m-%d')
            comm_mensuelle = math.floor(abo * taux)
            for i in range(duree):
                month_idx = (dt.month - 1 + 1 + i) % 12
                comm_vals[MONTHS[month_idx]] = comm_mensuelle
        except: pass
    return comm_vals, math.floor(abo)

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None
    try: return jwt.decode(auth[7:], SECRET, algorithms=['HS256'])
    except: return None

def recalc_all_commissions():
    prix = float(get_setting('prix_par_employe') or 200)
    conn = get_db()
    salons = conn.execute("""
        SELECT s.*, a.taux_commission, a.duree_commission
        FROM salons s JOIN affiliates a ON s.affiliate_id=a.id""").fetchall()
    for s in salons:
        comm_vals, _ = calc_commissions(s['nb_employes'], s['date_debut'],
                                        s['taux_commission'], s['duree_commission'], prix)
        set_clause = ', '.join([f'comm_{m}=?' for m in MONTHS])
        conn.execute(f"UPDATE commissions SET {set_clause} WHERE salon_id=?",
                     [comm_vals[m] for m in MONTHS] + [s['id']])
    conn.commit()
    conn.close()

# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=? AND password=?",
                        (data['email'], hash_pw(data['password']))).fetchone()
    conn.close()
    if not user: return jsonify({'error': 'Identifiants incorrects'}), 401
    token = jwt.encode({'id': user['id'], 'role': user['role'], 'email': user['email'],
                        'exp': datetime.utcnow() + timedelta(days=7)}, SECRET)
    return jsonify({'token': token, 'role': user['role'], 'email': user['email']})

@app.route('/api/me', methods=['GET'])
def me():
    payload = verify_token(request)
    if not payload: return jsonify({'error': 'Non autorisé'}), 401
    conn = get_db()
    if payload['role'] == 'affiliate':
        aff = conn.execute("SELECT * FROM affiliates WHERE user_id=?", (payload['id'],)).fetchone()
        conn.close()
        if not aff: return jsonify({'error': 'Affilié introuvable'}), 404
        return jsonify(dict(aff))
    conn.close()
    return jsonify({'role': 'admin', 'email': payload['email']})

# ── SETTINGS ──────────────────────────────────────────────────────────────────

@app.route('/api/settings', methods=['GET'])
def get_settings():
    payload = verify_token(request)
    if not payload: return jsonify({'error': 'Non autorisé'}), 401
    conn = get_db()
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    conn.close()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['PUT'])
def update_settings():
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    for key, value in data.items():
        conn.execute(UPSERT('settings','key,value','?,?','key','value=EXCLUDED.value' if IS_PG else 'value=?'),
                     (key, str(value)) if IS_PG else (key, str(value), str(value)))
    conn.commit(); conn.close()
    if 'prix_par_employe' in data: recalc_all_commissions()
    return jsonify({'ok': True})

# ── AFFILIÉS ──────────────────────────────────────────────────────────────────

@app.route('/api/affiliates', methods=['GET'])
def get_affiliates():
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    rows = conn.execute("SELECT a.*, u.email as user_email FROM affiliates a JOIN users u ON a.user_id=u.id").fetchall()
    result = []
    for a in rows:
        aff = dict(a)
        s = conn.execute("SELECT COUNT(*) as cnt FROM salons WHERE affiliate_id=?", (a['id'],)).fetchone()
        aff['nb_salons'] = s['cnt']
        comm_sum = '+'.join([f'c.comm_{m}' for m in MONTHS])
        ct = conn.execute(f"SELECT COALESCE(SUM({comm_sum}),0) as total FROM commissions c JOIN salons s ON c.salon_id=s.id WHERE s.affiliate_id=?", (a['id'],)).fetchone()
        aff['commission_totale'] = math.floor(ct['total'] or 0)
        vt = conn.execute("SELECT COALESCE(SUM(montant),0) as total FROM versements WHERE affiliate_id=?", (a['id'],)).fetchone()
        aff['commission_versee'] = math.floor(vt['total'] or 0)
        aff['commission_restante'] = aff['commission_totale'] - aff['commission_versee']
        result.append(aff)
    conn.close()
    return jsonify(result)

@app.route('/api/affiliates', methods=['POST'])
def create_affiliate():
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    try:
        pw = data.get('password', 'Affilie2024!')
        conn.execute(IGNORE('users','email,password,role','?,?,?','email'),
                     (data['email'], hash_pw(pw), 'affiliate'))
        conn.commit()
        user = conn.execute("SELECT id FROM users WHERE email=?", (data['email'],)).fetchone()
        user_id = user['id']
        code = 'AFF' + str(100 + user_id)
        tok = secrets.token_urlsafe(24)
        conn.execute('''INSERT INTO affiliates
            (user_id,nom,prenom,email,telephone,instagram,taux_commission,
             duree_commission,date_adhesion,statut,code_parrain,token_public)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (user_id, data.get('nom',''), data.get('prenom',''), data['email'],
             data.get('telephone',''), data.get('instagram',''),
             float(data.get('taux_commission',0.3)), int(data.get('duree_commission',3)),
             data.get('date_adhesion', datetime.now().strftime('%Y-%m-%d')),
             data.get('statut','Actif'), code, tok))
        conn.commit()
        aff_id = conn.lastrowid
        conn.close()
        return jsonify({'id': aff_id, 'code_parrain': code, 'password_temp': pw}), 201
    except Exception as e:
        conn.close()
        return jsonify({'error': 'Email déjà utilisé'}), 409

@app.route('/api/affiliates/<int:aid>', methods=['PUT'])
def update_affiliate(aid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    conn.execute('''UPDATE affiliates SET nom=?,prenom=?,telephone=?,instagram=?,
                    taux_commission=?,duree_commission=?,date_adhesion=?,statut=? WHERE id=?''',
                 (data.get('nom'), data.get('prenom'), data.get('telephone'),
                  data.get('instagram'), float(data.get('taux_commission',0.3)),
                  int(data.get('duree_commission',3)), data.get('date_adhesion'),
                  data.get('statut'), aid))
    if data.get('password'):
        aff = conn.execute("SELECT user_id FROM affiliates WHERE id=?", (aid,)).fetchone()
        if aff: conn.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(data['password']), aff['user_id']))
    conn.commit()
    prix = float(get_setting('prix_par_employe') or 200)
    salons = conn.execute("SELECT * FROM salons WHERE affiliate_id=?", (aid,)).fetchall()
    taux = float(data.get('taux_commission', 0.3))
    duree = int(data.get('duree_commission', 3))
    for s in salons:
        comm_vals, _ = calc_commissions(s['nb_employes'], s['date_debut'], taux, duree, prix)
        set_clause = ', '.join([f'comm_{m}=?' for m in MONTHS])
        conn.execute(f"UPDATE commissions SET {set_clause} WHERE salon_id=?",
                     [comm_vals[m] for m in MONTHS] + [s['id']])
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/affiliates/<int:aid>', methods=['DELETE'])
def delete_affiliate(aid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    aff = conn.execute("SELECT user_id FROM affiliates WHERE id=?", (aid,)).fetchone()
    if aff:
        for s in conn.execute("SELECT id FROM salons WHERE affiliate_id=?", (aid,)).fetchall():
            conn.execute("DELETE FROM commissions WHERE salon_id=?", (s['id'],))
        conn.execute("DELETE FROM salons WHERE affiliate_id=?", (aid,))
        conn.execute("DELETE FROM versements WHERE affiliate_id=?", (aid,))
        conn.execute("DELETE FROM affiliates WHERE id=?", (aid,))
        conn.execute("DELETE FROM users WHERE id=?", (aff['user_id'],))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── SALONS ────────────────────────────────────────────────────────────────────

@app.route('/api/affiliates/<int:aid>/salons', methods=['GET'])
def get_salons(aid):
    payload = verify_token(request)
    if not payload: return jsonify({'error': 'Non autorisé'}), 401
    if payload['role'] == 'affiliate':
        conn = get_db()
        aff = conn.execute("SELECT id FROM affiliates WHERE user_id=?", (payload['id'],)).fetchone()
        conn.close()
        if not aff or aff['id'] != aid: return jsonify({'error': 'Accès refusé'}), 403
    prix = float(get_setting('prix_par_employe') or 200)
    conn = get_db()
    salons = conn.execute("SELECT * FROM salons WHERE affiliate_id=?", (aid,)).fetchall()
    aff = conn.execute("SELECT taux_commission,duree_commission FROM affiliates WHERE id=?", (aid,)).fetchone()
    result = []
    for s in salons:
        salon = dict(s)
        salon['abonnement'] = (s['nb_employes'] or 0) * prix
        salon['taux_commission'] = aff['taux_commission'] if aff else 0.3
        salon['duree_commission'] = aff['duree_commission'] if aff else 3
        salon['prix_par_employe'] = prix
        comm = conn.execute("SELECT * FROM commissions WHERE salon_id=?", (s['id'],)).fetchone()
        salon['commissions'] = dict(comm) if comm else {}
        result.append(salon)
    conn.close()
    return jsonify(result)

@app.route('/api/affiliates/<int:aid>/salons', methods=['POST'])
def add_salon(aid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    prix = float(get_setting('prix_par_employe') or 200)
    conn = get_db()
    aff = conn.execute("SELECT taux_commission,duree_commission FROM affiliates WHERE id=?", (aid,)).fetchone()
    if not aff: conn.close(); return jsonify({'error': 'Affilié introuvable'}), 404
    nb_emp = int(data.get('nb_employes', 0))
    date_debut = data.get('date_debut', '')
    conn.execute("INSERT INTO salons (affiliate_id,nom,ville,nb_employes,date_debut) VALUES (?,?,?,?,?)",
                 (aid, data.get('nom',''), data.get('ville',''), nb_emp, date_debut))
    conn.commit()
    salon_id = conn.lastrowid
    comm_vals, _ = calc_commissions(nb_emp, date_debut, aff['taux_commission'], aff['duree_commission'], prix)
    cols = ', '.join([f'comm_{m}, paid_{m}' for m in MONTHS])
    vals = []
    for m in MONTHS: vals += [comm_vals[m], 0]
    ph = ', '.join(['?,?' for _ in MONTHS])
    conn.execute(f"INSERT INTO commissions (salon_id, annee, {cols}) VALUES (?, 2026, {ph})", [salon_id] + vals)
    conn.commit(); conn.close()
    return jsonify({'id': salon_id}), 201

@app.route('/api/salons/<int:sid>', methods=['PUT'])
def update_salon(sid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    prix = float(get_setting('prix_par_employe') or 200)
    conn = get_db()
    nb_emp = int(data.get('nb_employes', 0))
    date_debut = data.get('date_debut', '')
    conn.execute("UPDATE salons SET nom=?,ville=?,nb_employes=?,date_debut=? WHERE id=?",
                 (data.get('nom'), data.get('ville'), nb_emp, date_debut, sid))
    salon = conn.execute("SELECT affiliate_id FROM salons WHERE id=?", (sid,)).fetchone()
    if salon:
        aff = conn.execute("SELECT taux_commission,duree_commission FROM affiliates WHERE id=?",
                           (salon['affiliate_id'],)).fetchone()
        comm_vals, _ = calc_commissions(nb_emp, date_debut, aff['taux_commission'], aff['duree_commission'], prix)
        set_clause = ', '.join([f'comm_{m}=?' for m in MONTHS])
        conn.execute(f"UPDATE commissions SET {set_clause} WHERE salon_id=?",
                     [comm_vals[m] for m in MONTHS] + [sid])
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/salons/<int:sid>', methods=['DELETE'])
def delete_salon(sid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    conn.execute("DELETE FROM commissions WHERE salon_id=?", (sid,))
    conn.execute("DELETE FROM salons WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/commissions/<int:sid>', methods=['PUT'])
def update_commission(sid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    updates, vals = [], []
    for m in MONTHS:
        if f'paid_{m}' in data:
            updates.append(f'paid_{m}=?'); vals.append(1 if data[f'paid_{m}'] else 0)
        if f'comm_{m}' in data:
            updates.append(f'comm_{m}=?'); vals.append(float(data[f'comm_{m}']))
    if updates:
        conn.execute(f"UPDATE commissions SET {','.join(updates)} WHERE salon_id=?", vals + [sid])
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── VERSEMENTS ────────────────────────────────────────────────────────────────

@app.route('/api/affiliates/<int:aid>/versements', methods=['GET'])
def get_versements(aid):
    payload = verify_token(request)
    if not payload: return jsonify({'error': 'Non autorisé'}), 401
    conn = get_db()
    rows = conn.execute("SELECT * FROM versements WHERE affiliate_id=? ORDER BY date DESC", (aid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/affiliates/<int:aid>/versements', methods=['POST'])
def add_versement(aid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    conn.execute("INSERT INTO versements (affiliate_id,montant,date,note) VALUES (?,?,?,?)",
                 (aid, float(data['montant']), data.get('date', datetime.now().strftime('%Y-%m-%d')), data.get('note','')))
    conn.commit(); conn.close()
    return jsonify({'ok': True}), 201

@app.route('/api/versements/<int:vid>', methods=['DELETE'])
def delete_versement(vid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    conn.execute("DELETE FROM versements WHERE id=?", (vid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── STATS ─────────────────────────────────────────────────────────────────────

@app.route('/api/stats', methods=['GET'])
def get_stats():
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    ta = conn.execute("SELECT COUNT(*) as c FROM affiliates WHERE statut='Actif'").fetchone()['c']
    ts = conn.execute("SELECT COUNT(*) as c FROM salons").fetchone()['c']
    comm_sum = '+'.join([f'comm_{m}' for m in MONTHS])
    tc = conn.execute(f"SELECT COALESCE(SUM({comm_sum}),0) as t FROM commissions").fetchone()['t']
    tv = conn.execute("SELECT COALESCE(SUM(montant),0) as t FROM versements").fetchone()['t']
    monthly = []
    for i, m in enumerate(MONTHS):
        val = conn.execute(f"SELECT COALESCE(SUM(comm_{m}),0) as t FROM commissions").fetchone()['t']
        monthly.append({'mois': MONTH_LABELS[i], 'commission': math.floor(val)})
    top = conn.execute(f"""
        SELECT a.prenom, a.nom, COALESCE(SUM({comm_sum}),0) as total
        FROM affiliates a LEFT JOIN salons s ON s.affiliate_id=a.id
        LEFT JOIN commissions c ON c.salon_id=s.id
        GROUP BY a.id, a.prenom, a.nom ORDER BY total DESC LIMIT 5""").fetchall()
    conn.close()
    return jsonify({
        'affilies_actifs': ta, 'salons_total': ts,
        'commissions_totales': math.floor(tc),
        'commissions_versees': math.floor(tv),
        'commissions_restantes': math.floor(tc - tv),
        'evolution_mensuelle': monthly,
        'top_affilies': [{'nom': r['prenom']+' '+r['nom'], 'total': math.floor(r['total'])} for r in top]
    })

# ── LIEN PUBLIC ───────────────────────────────────────────────────────────────

@app.route('/api/public/<token>', methods=['GET'])
def public_view(token):
    conn = get_db()
    aff = conn.execute("SELECT * FROM affiliates WHERE token_public=?", (token,)).fetchone()
    if not aff: conn.close(); return jsonify({'error': 'Lien invalide'}), 404
    prix = float(get_setting('prix_par_employe') or 200)
    salons = conn.execute("SELECT * FROM salons WHERE affiliate_id=?", (aff['id'],)).fetchall()
    salons_list = []
    for s in salons:
        salon = dict(s)
        salon['abonnement'] = (s['nb_employes'] or 0) * prix
        comm = conn.execute("SELECT * FROM commissions WHERE salon_id=?", (s['id'],)).fetchone()
        salon['commissions'] = dict(comm) if comm else {}
        salons_list.append(salon)
    versements = conn.execute("SELECT * FROM versements WHERE affiliate_id=? ORDER BY date DESC", (aff['id'],)).fetchall()
    comm_sum = '+'.join([f'c.comm_{m}' for m in MONTHS])
    tc = conn.execute(f"SELECT COALESCE(SUM({comm_sum}),0) as t FROM commissions c JOIN salons s ON c.salon_id=s.id WHERE s.affiliate_id=?", (aff['id'],)).fetchone()['t']
    tv = conn.execute("SELECT COALESCE(SUM(montant),0) as t FROM versements WHERE affiliate_id=?", (aff['id'],)).fetchone()['t']
    conn.close()
    return jsonify({
        'affilié': {'prenom': aff['prenom'], 'nom': aff['nom'], 'instagram': aff['instagram'],
                    'code_parrain': aff['code_parrain'], 'taux_commission': aff['taux_commission'],
                    'duree_commission': aff['duree_commission'], 'date_adhesion': aff['date_adhesion'],
                    'statut': aff['statut']},
        'salons': salons_list,
        'versements': [dict(v) for v in versements],
        'totaux': {'commission_totale': math.floor(tc), 'commission_versee': math.floor(tv),
                   'commission_restante': math.floor(tc - tv), 'nb_salons': len(salons_list)}
    })

@app.route('/api/affiliates/<int:aid>/regenerate-token', methods=['POST'])
def regenerate_token(aid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin': return jsonify({'error': 'Accès refusé'}), 403
    new_token = secrets.token_urlsafe(24)
    conn = get_db()
    conn.execute("UPDATE affiliates SET token_public=? WHERE id=?", (new_token, aid))
    conn.commit(); conn.close()
    return jsonify({'token_public': new_token})

# ── PASSWORD ──────────────────────────────────────────────────────────────────

@app.route('/api/change-password', methods=['POST'])
def change_password():
    payload = verify_token(request)
    if not payload: return jsonify({'error': 'Non autorisé'}), 401
    data = request.json
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (payload['id'],)).fetchone()
    if not user or user['password'] != hash_pw(data.get('old_password','')):
        conn.close(); return jsonify({'error': 'Mot de passe actuel incorrect'}), 400
    conn.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(data['new_password']), payload['id']))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── STATIC ────────────────────────────────────────────────────────────────────

@app.route('/p/<token>')
def public_page(token):
    return send_from_directory('static', 'public.html')

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    if path and os.path.exists(os.path.join('static', path)):
        return send_from_directory('static', path)
    return send_from_directory('static', 'index.html')

# ── START ─────────────────────────────────────────────────────────────────────

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n🐝 hibee Affiliates — http://localhost:{port}")
    print(f"   Base : {'PostgreSQL (Supabase)' if IS_PG else 'SQLite (local)'}\n")
    app.run(debug=False, host='0.0.0.0', port=port)
