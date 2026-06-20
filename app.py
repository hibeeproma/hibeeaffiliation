import sqlite3, hashlib, jwt, json, os, math, secrets
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)
SECRET = 'hibee-secret-key-2024-affiliates'
DB = 'hibee.db'

MONTHS = ['janv','fevr','mars','avr','mai','juin','juil','aout','sept','oct','nov','dec']
MONTH_LABELS = ['Janvier','Février','Mars','Avril','Mai','Juin','Juillet','Août','Septembre','Octobre','Novembre','Décembre']

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')
    c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('prix_par_employe','200')")
    c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('annee_suivi','2026')")

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'affiliate'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS affiliates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        nom TEXT, prenom TEXT, email TEXT, telephone TEXT,
        instagram TEXT, taux_commission REAL DEFAULT 0.3,
        duree_commission INTEGER DEFAULT 3,
        date_adhesion TEXT, statut TEXT DEFAULT 'Actif',
        code_parrain TEXT UNIQUE,
        token_public TEXT UNIQUE
    )''')
    conn.commit()
    # Migration : ajouter token_public si absente
    cols = [r[1] for r in c.execute("PRAGMA table_info(affiliates)").fetchall()]
    if 'token_public' not in cols:
        c.execute("ALTER TABLE affiliates ADD COLUMN token_public TEXT")
        conn.commit()
    c.execute('''CREATE TABLE IF NOT EXISTS salons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        affiliate_id INTEGER REFERENCES affiliates(id) ON DELETE CASCADE,
        nom TEXT, ville TEXT, nb_employes INTEGER DEFAULT 0,
        date_debut TEXT
    )''')
    monthly_cols = ' '.join([f', comm_{m} REAL DEFAULT 0, paid_{m} INTEGER DEFAULT 0' for m in MONTHS])
    c.execute(f'''CREATE TABLE IF NOT EXISTS commissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        salon_id INTEGER UNIQUE REFERENCES salons(id) ON DELETE CASCADE,
        annee INTEGER DEFAULT 2026
        {monthly_cols}
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS versements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        affiliate_id INTEGER REFERENCES affiliates(id) ON DELETE CASCADE,
        montant REAL, date TEXT, note TEXT
    )''')

    # Générer un token pour les affiliés qui n'en ont pas
    conn.commit()
    affs_sans_token = c.execute("SELECT id FROM affiliates WHERE token_public IS NULL").fetchall()
    for a in affs_sans_token:
        c.execute("UPDATE affiliates SET token_public=? WHERE id=?",
                  (secrets.token_urlsafe(24), a['id']))

    # Admin
    c.execute("INSERT OR IGNORE INTO users (email,password,role) VALUES (?,?,?)",
              ('admin@hibee.ma', hash_pw('Admin2024!'), 'admin'))
    # Affilié démo
    c.execute("INSERT OR IGNORE INTO users (email,password,role) VALUES (?,?,?)",
              ('affilie1@hibee.ma', hash_pw('Affilie2024!'), 'affiliate'))
    conn.commit()
    user = c.execute("SELECT id FROM users WHERE email='affilie1@hibee.ma'").fetchone()
    if user:
        existing = c.execute("SELECT id FROM affiliates WHERE user_id=?", (user['id'],)).fetchone()
        if not existing:
            c.execute('''INSERT INTO affiliates (user_id,nom,prenom,email,telephone,instagram,
                         taux_commission,duree_commission,date_adhesion,statut,code_parrain,token_public)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (user['id'],'Ben Youssef','Sarbine','affilie1@hibee.ma',
                       '+212 6XX XXX XXX','@influenceur1',0.3,3,'2026-01-01','Actif','AFF001',
                       secrets.token_urlsafe(24)))
            conn.commit()
    conn.close()

def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else None

def calc_commissions(nb_employes, date_debut, taux, duree, prix_par_employe):
    """
    Abonnement = nb_employes × prix_par_employe
    Commission démarre le mois SUIVANT le début d'abonnement
    Ex: début 20/02/2026 → commissions en mars, avril, mai (si duree=3)
    """
    abo = nb_employes * prix_par_employe
    comm_vals = {m: 0.0 for m in MONTHS}
    if date_debut and abo > 0 and duree > 0:
        try:
            dt = datetime.strptime(date_debut, '%Y-%m-%d')
            comm_mensuelle = math.floor(abo * taux)
            for i in range(duree):
                # +1 car commission démarre le mois suivant
                month_idx = (dt.month - 1 + 1 + i) % 12
                comm_vals[MONTHS[month_idx]] = comm_mensuelle
        except:
            pass
    return comm_vals, round(abo, 2)

def verify_token(req):
    auth = req.headers.get('Authorization','')
    if not auth.startswith('Bearer '):
        return None
    try:
        return jwt.decode(auth[7:], SECRET, algorithms=['HS256'])
    except:
        return None

# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=? AND password=?",
                        (data['email'], hash_pw(data['password']))).fetchone()
    conn.close()
    if not user:
        return jsonify({'error': 'Identifiants incorrects'}), 401
    token = jwt.encode({'id': user['id'], 'role': user['role'], 'email': user['email'],
                        'exp': datetime.utcnow() + timedelta(days=7)}, SECRET)
    return jsonify({'token': token, 'role': user['role'], 'email': user['email']})

@app.route('/api/me', methods=['GET'])
def me():
    payload = verify_token(request)
    if not payload:
        return jsonify({'error': 'Non autorisé'}), 401
    conn = get_db()
    if payload['role'] == 'affiliate':
        aff = conn.execute("SELECT * FROM affiliates WHERE user_id=?", (payload['id'],)).fetchone()
        conn.close()
        if not aff:
            return jsonify({'error': 'Affilié introuvable'}), 404
        return jsonify(dict(aff))
    conn.close()
    return jsonify({'role': 'admin', 'email': payload['email']})

# ── SETTINGS ──────────────────────────────────────────────────────────────────

@app.route('/api/settings', methods=['GET'])
def get_settings():
    payload = verify_token(request)
    if not payload:
        return jsonify({'error': 'Non autorisé'}), 401
    conn = get_db()
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    conn.close()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['PUT'])
def update_settings():
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    for key, value in data.items():
        conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()
    # Recalculer toutes les commissions avec le nouveau prix
    if 'prix_par_employe' in data:
        recalc_all_commissions()
    return jsonify({'ok': True})

def recalc_all_commissions():
    prix = float(get_setting('prix_par_employe') or 200)
    conn = get_db()
    salons = conn.execute("""
        SELECT s.*, a.taux_commission, a.duree_commission
        FROM salons s JOIN affiliates a ON s.affiliate_id=a.id
    """).fetchall()
    for s in salons:
        comm_vals, _ = calc_commissions(
            s['nb_employes'], s['date_debut'],
            s['taux_commission'], s['duree_commission'], prix)
        set_clause = ', '.join([f'comm_{m}=?' for m in MONTHS])
        vals = [comm_vals[m] for m in MONTHS]
        conn.execute(f"UPDATE commissions SET {set_clause} WHERE salon_id=?", vals + [s['id']])
    conn.commit()
    conn.close()

# ── ADMIN: AFFILIÉS ───────────────────────────────────────────────────────────

@app.route('/api/affiliates', methods=['GET'])
def get_affiliates():
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    rows = conn.execute("SELECT a.*, u.email as user_email FROM affiliates a JOIN users u ON a.user_id=u.id").fetchall()
    result = []
    for a in rows:
        aff = dict(a)
        salons_count = conn.execute("SELECT COUNT(*) as cnt FROM salons WHERE affiliate_id=?", (a['id'],)).fetchone()
        aff['nb_salons'] = salons_count['cnt']
        comm_sum = '+'.join([f'c.comm_{m}' for m in MONTHS])
        comm_total = conn.execute(f"""
            SELECT COALESCE(SUM({comm_sum}),0) as total
            FROM commissions c JOIN salons s ON c.salon_id=s.id WHERE s.affiliate_id=?
        """, (a['id'],)).fetchone()
        aff['commission_totale'] = round(comm_total['total'] or 0, 2)
        versements = conn.execute("SELECT COALESCE(SUM(montant),0) as total FROM versements WHERE affiliate_id=?", (a['id'],)).fetchone()
        aff['commission_versee'] = round(versements['total'] or 0, 2)
        aff['commission_restante'] = round(aff['commission_totale'] - aff['commission_versee'], 2)
        result.append(aff)
    conn.close()
    return jsonify(result)

@app.route('/api/affiliates', methods=['POST'])
def create_affiliate():
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    try:
        pw = data.get('password', 'Affilie2024!')
        c = conn.cursor()
        c.execute("INSERT INTO users (email,password,role) VALUES (?,?,?)",
                  (data['email'], hash_pw(pw), 'affiliate'))
        user_id = c.lastrowid
        code = 'AFF' + str(100 + user_id)
        token_pub = secrets.token_urlsafe(24)
        c.execute('''INSERT INTO affiliates (user_id,nom,prenom,email,telephone,instagram,
                     taux_commission,duree_commission,date_adhesion,statut,code_parrain,token_public)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (user_id, data.get('nom',''), data.get('prenom',''), data['email'],
                   data.get('telephone',''), data.get('instagram',''),
                   float(data.get('taux_commission', 0.3)),
                   int(data.get('duree_commission', 3)),
                   data.get('date_adhesion', datetime.now().strftime('%Y-%m-%d')),
                   data.get('statut','Actif'), code, token_pub))
        conn.commit()
        aff_id = c.lastrowid
        conn.close()
        return jsonify({'id': aff_id, 'code_parrain': code, 'password_temp': pw}), 201
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Email déjà utilisé'}), 409

@app.route('/api/affiliates/<int:aid>', methods=['PUT'])
def update_affiliate(aid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    conn.execute('''UPDATE affiliates SET nom=?,prenom=?,telephone=?,instagram=?,
                    taux_commission=?,duree_commission=?,date_adhesion=?,statut=? WHERE id=?''',
                 (data.get('nom'), data.get('prenom'), data.get('telephone'),
                  data.get('instagram'), float(data.get('taux_commission',0.3)),
                  int(data.get('duree_commission',3)),
                  data.get('date_adhesion'), data.get('statut'), aid))
    if data.get('password'):
        aff = conn.execute("SELECT user_id FROM affiliates WHERE id=?", (aid,)).fetchone()
        if aff:
            conn.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(data['password']), aff['user_id']))
    conn.commit()
    # Recalculer les commissions de cet affilié
    prix = float(get_setting('prix_par_employe') or 200)
    salons = conn.execute("SELECT * FROM salons WHERE affiliate_id=?", (aid,)).fetchall()
    taux = float(data.get('taux_commission', 0.3))
    duree = int(data.get('duree_commission', 3))
    for s in salons:
        comm_vals, _ = calc_commissions(s['nb_employes'], s['date_debut'], taux, duree, prix)
        set_clause = ', '.join([f'comm_{m}=?' for m in MONTHS])
        conn.execute(f"UPDATE commissions SET {set_clause} WHERE salon_id=?",
                     [comm_vals[m] for m in MONTHS] + [s['id']])
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/affiliates/<int:aid>', methods=['DELETE'])
def delete_affiliate(aid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    aff = conn.execute("SELECT user_id FROM affiliates WHERE id=?", (aid,)).fetchone()
    if aff:
        conn.execute("DELETE FROM affiliates WHERE id=?", (aid,))
        conn.execute("DELETE FROM users WHERE id=?", (aff['user_id'],))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── SALONS ────────────────────────────────────────────────────────────────────

@app.route('/api/affiliates/<int:aid>/salons', methods=['GET'])
def get_salons(aid):
    payload = verify_token(request)
    if not payload:
        return jsonify({'error': 'Non autorisé'}), 401
    if payload['role'] == 'affiliate':
        conn = get_db()
        aff = conn.execute("SELECT id FROM affiliates WHERE user_id=?", (payload['id'],)).fetchone()
        conn.close()
        if not aff or aff['id'] != aid:
            return jsonify({'error': 'Accès refusé'}), 403
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
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    prix = float(get_setting('prix_par_employe') or 200)
    conn = get_db()
    aff = conn.execute("SELECT taux_commission,duree_commission FROM affiliates WHERE id=?", (aid,)).fetchone()
    if not aff:
        conn.close()
        return jsonify({'error': 'Affilié introuvable'}), 404
    nb_emp = int(data.get('nb_employes', 0))
    date_debut = data.get('date_debut', '')
    c = conn.cursor()
    c.execute('''INSERT INTO salons (affiliate_id,nom,ville,nb_employes,date_debut)
                 VALUES (?,?,?,?,?)''',
              (aid, data.get('nom',''), data.get('ville',''), nb_emp, date_debut))
    salon_id = c.lastrowid
    comm_vals, _ = calc_commissions(nb_emp, date_debut, aff['taux_commission'], aff['duree_commission'], prix)
    cols = ', '.join([f'comm_{m}, paid_{m}' for m in MONTHS])
    vals = []
    for m in MONTHS:
        vals += [comm_vals[m], 0]
    placeholders = ', '.join(['?,?' for _ in MONTHS])
    c.execute(f"INSERT INTO commissions (salon_id, annee, {cols}) VALUES (?, 2026, {placeholders})",
              [salon_id] + vals)
    conn.commit()
    conn.close()
    return jsonify({'id': salon_id}), 201

@app.route('/api/salons/<int:sid>', methods=['PUT'])
def update_salon(sid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
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
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/salons/<int:sid>', methods=['DELETE'])
def delete_salon(sid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    conn.execute("DELETE FROM salons WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/commissions/<int:sid>', methods=['PUT'])
def update_commission(sid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    updates, vals = [], []
    for m in MONTHS:
        if f'paid_{m}' in data:
            updates.append(f'paid_{m}=?')
            vals.append(1 if data[f'paid_{m}'] else 0)
        if f'comm_{m}' in data:
            updates.append(f'comm_{m}=?')
            vals.append(float(data[f'comm_{m}']))
    if updates:
        conn.execute(f"UPDATE commissions SET {','.join(updates)} WHERE salon_id=?", vals + [sid])
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── VERSEMENTS ────────────────────────────────────────────────────────────────

@app.route('/api/affiliates/<int:aid>/versements', methods=['GET'])
def get_versements(aid):
    payload = verify_token(request)
    if not payload:
        return jsonify({'error': 'Non autorisé'}), 401
    conn = get_db()
    rows = conn.execute("SELECT * FROM versements WHERE affiliate_id=? ORDER BY date DESC", (aid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/affiliates/<int:aid>/versements', methods=['POST'])
def add_versement(aid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    data = request.json
    conn = get_db()
    conn.execute("INSERT INTO versements (affiliate_id,montant,date,note) VALUES (?,?,?,?)",
                 (aid, float(data['montant']), data.get('date', datetime.now().strftime('%Y-%m-%d')), data.get('note','')))
    conn.commit()
    conn.close()
    return jsonify({'ok': True}), 201

@app.route('/api/versements/<int:vid>', methods=['DELETE'])
def delete_versement(vid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    conn.execute("DELETE FROM versements WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── STATS ─────────────────────────────────────────────────────────────────────

@app.route('/api/stats', methods=['GET'])
def get_stats():
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    conn = get_db()
    total_affilies = conn.execute("SELECT COUNT(*) as c FROM affiliates WHERE statut='Actif'").fetchone()['c']
    total_salons = conn.execute("SELECT COUNT(*) as c FROM salons").fetchone()['c']
    comm_sum = '+'.join([f'comm_{m}' for m in MONTHS])
    total_comm = conn.execute(f"SELECT COALESCE(SUM({comm_sum}),0) as t FROM commissions").fetchone()['t']
    total_verse = conn.execute("SELECT COALESCE(SUM(montant),0) as t FROM versements").fetchone()['t']
    monthly = []
    for i, m in enumerate(MONTHS):
        val = conn.execute(f"SELECT COALESCE(SUM(comm_{m}),0) as t FROM commissions").fetchone()['t']
        monthly.append({'mois': MONTH_LABELS[i], 'commission': round(val, 2)})
    # Top affiliés
    top = conn.execute(f"""
        SELECT a.prenom, a.nom, COALESCE(SUM({comm_sum}),0) as total
        FROM affiliates a
        LEFT JOIN salons s ON s.affiliate_id=a.id
        LEFT JOIN commissions c ON c.salon_id=s.id
        GROUP BY a.id ORDER BY total DESC LIMIT 5
    """).fetchall()
    conn.close()
    return jsonify({
        'affilies_actifs': total_affilies,
        'salons_total': total_salons,
        'commissions_totales': round(total_comm, 2),
        'commissions_versees': round(total_verse, 2),
        'commissions_restantes': round(total_comm - total_verse, 2),
        'evolution_mensuelle': monthly,
        'top_affilies': [{'nom': r['prenom']+' '+r['nom'], 'total': round(r['total'],2)} for r in top]
    })

# ── LIEN PUBLIC (lecture seule) ───────────────────────────────────────────────

@app.route('/api/public/<token>', methods=['GET'])
def public_view(token):
    conn = get_db()
    aff = conn.execute("SELECT * FROM affiliates WHERE token_public=?", (token,)).fetchone()
    if not aff:
        conn.close()
        return jsonify({'error': 'Lien invalide ou expiré'}), 404
    prix = float(get_setting('prix_par_employe') or 200)
    salons = conn.execute("SELECT * FROM salons WHERE affiliate_id=?", (aff['id'],)).fetchall()
    salons_list = []
    for s in salons:
        salon = dict(s)
        salon['abonnement'] = (s['nb_employes'] or 0) * prix
        comm = conn.execute("SELECT * FROM commissions WHERE salon_id=?", (s['id'],)).fetchone()
        salon['commissions'] = dict(comm) if comm else {}
        salons_list.append(salon)
    versements = conn.execute(
        "SELECT * FROM versements WHERE affiliate_id=? ORDER BY date DESC", (aff['id'],)
    ).fetchall()
    comm_sum = '+'.join([f'c.comm_{m}' for m in MONTHS])
    total_comm = conn.execute(f"""
        SELECT COALESCE(SUM({comm_sum}),0) as t
        FROM commissions c JOIN salons s ON c.salon_id=s.id WHERE s.affiliate_id=?
    """, (aff['id'],)).fetchone()['t']
    total_verse = conn.execute(
        "SELECT COALESCE(SUM(montant),0) as t FROM versements WHERE affiliate_id=?", (aff['id'],)
    ).fetchone()['t']
    conn.close()
    return jsonify({
        'affilié': {
            'prenom': aff['prenom'], 'nom': aff['nom'],
            'instagram': aff['instagram'], 'code_parrain': aff['code_parrain'],
            'taux_commission': aff['taux_commission'],
            'duree_commission': aff['duree_commission'],
            'date_adhesion': aff['date_adhesion'], 'statut': aff['statut'],
        },
        'salons': salons_list,
        'versements': [dict(v) for v in versements],
        'totaux': {
            'commission_totale': math.floor(total_comm),
            'commission_versee': math.floor(total_verse),
            'commission_restante': math.floor(total_comm - total_verse),
            'nb_salons': len(salons_list),
        }
    })

@app.route('/api/affiliates/<int:aid>/regenerate-token', methods=['POST'])
def regenerate_token(aid):
    payload = verify_token(request)
    if not payload or payload['role'] != 'admin':
        return jsonify({'error': 'Accès refusé'}), 403
    new_token = secrets.token_urlsafe(24)
    conn = get_db()
    conn.execute("UPDATE affiliates SET token_public=? WHERE id=?", (new_token, aid))
    conn.commit()
    conn.close()
    return jsonify({'token_public': new_token})

# ── PASSWORD ──────────────────────────────────────────────────────────────────

@app.route('/api/change-password', methods=['POST'])
def change_password():
    payload = verify_token(request)
    if not payload:
        return jsonify({'error': 'Non autorisé'}), 401
    data = request.json
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (payload['id'],)).fetchone()
    if not user or user['password'] != hash_pw(data.get('old_password','')):
        conn.close()
        return jsonify({'error': 'Mot de passe actuel incorrect'}), 400
    conn.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(data['new_password']), payload['id']))
    conn.commit()
    conn.close()
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

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n🐝 hibee Affiliates — http://localhost:{port}")
    print("   Admin  : admin@hibee.ma / Admin2024!")
    print("   Affilié: affilie1@hibee.ma / Affilie2024!\n")
    app.run(debug=False, host='0.0.0.0', port=port)
