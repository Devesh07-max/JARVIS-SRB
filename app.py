import os
from flask import Flask, jsonify, render_template_string, request

from safe_repo.core.payment_service import (
    read_checkout_token, create_order, verify_checkout_payment,
    verify_webhook_signature, process_webhook_event,
)

app = Flask(__name__)

CHECKOUT_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JARVIS Premium Checkout</title><script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;background:#eef3f8;color:#172033;font-family:Arial,sans-serif;min-height:100vh;display:grid;place-items:center;padding:18px}
.card{width:min(100%,460px);background:#fff;border-radius:22px;padding:26px;box-shadow:0 14px 45px #15233a18}.brand{font-weight:800;font-size:22px;letter-spacing:.3px}.muted{color:#65728a;line-height:1.5}.plan{background:#f4f7fb;border-radius:15px;padding:17px;margin:20px 0}.label{font-size:13px;color:#65728a}.amount{font-size:34px;font-weight:800;margin:7px 0}.row{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:14px 0}.switch{display:flex;background:#edf1f6;padding:4px;border-radius:12px}.switch button{border:0;border-radius:9px;padding:9px 15px;font-weight:700;background:transparent;color:#526078}.switch button.active{background:#fff;color:#172033;box-shadow:0 1px 5px #0001}.pay{width:100%;border:0;border-radius:13px;background:#1769e0;color:#fff;font-size:17px;font-weight:800;padding:16px;cursor:pointer}.pay:disabled{opacity:.55;cursor:wait}.note{font-size:12px;color:#748096;margin-top:15px;line-height:1.5}.status{margin-top:16px;padding:12px;border-radius:10px;display:none;line-height:1.5}.ok{background:#e8f8ee;color:#176536}.bad{background:#fff0ee;color:#9e2d20}.hidden{display:none}
</style></head><body><main class="card">
<div class="brand">⚡ JARVIS Batches</div><p class="muted">Secure Premium subscription checkout</p>
<div class="plan"><div class="label">SELECTED PLAN</div><h2 id="planName" style="margin:6px 0 2px"></h2><div class="muted" id="duration"></div><div class="amount" id="amount">—</div><div class="label">Your subscription activates automatically after successful payment verification.</div></div>
<div class="row"><b>Payment currency</b><div class="switch"><button id="inr" class="active" onclick="setCurrency('INR')">INR ₹</button><button id="usd" onclick="setCurrency('USD')">USD $</button></div></div>
<button class="pay" id="pay" onclick="startPayment()">Pay securely with Razorpay</button>
<div id="status" class="status"></div>
<p class="note">Indian customers can use UPI and other methods enabled on the merchant account. International customers can use supported international methods. Availability depends on your country, bank and Razorpay account settings. Do not close this page until Razorpay confirms the payment.</p>
</main><script>
const token={{ token|tojson }}; const keyId={{ key_id|tojson }};
const plan={{ plan_label|tojson }}; const duration={{ duration|tojson }};
const prices={{ prices|tojson }}; let currency='INR'; let busy=false;
document.getElementById('planName').textContent=plan;
document.getElementById('duration').textContent=duration+' subscription';
function money(){const v=prices[currency];document.getElementById('amount').textContent=currency==='INR'?'₹'+v.toLocaleString('en-IN'):'$'+Number(v).toFixed(2);}
function setCurrency(c){currency=c;document.getElementById('inr').classList.toggle('active',c==='INR');document.getElementById('usd').classList.toggle('active',c==='USD');money();}
function show(msg,good){const el=document.getElementById('status');el.style.display='block';el.className='status '+(good?'ok':'bad');el.textContent=msg;}
// IP-based suggestion only; users can always override it manually.
(async()=>{try{const r=await fetch('https://ipapi.co/json/');const d=await r.json();if(d&&d.country_code&&d.country_code!=='IN')setCurrency('USD');}catch(e){}})();
async function startPayment(){if(busy)return;busy=true;const btn=document.getElementById('pay');btn.disabled=true;btn.textContent='Creating secure order…';
try{const r=await fetch('/api/payment/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,currency})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not create payment order.');
const options={key:keyId,amount:data.amount,currency:data.currency,name:'JARVIS Batches',description:plan+' - '+duration,order_id:data.order_id,theme:{color:'#1769e0'},handler:async function(response){btn.textContent='Verifying payment…';try{const vr=await fetch('/api/payment/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,razorpay_order_id:response.razorpay_order_id,razorpay_payment_id:response.razorpay_payment_id,razorpay_signature:response.razorpay_signature})});const result=await vr.json();if(!vr.ok)throw new Error(result.error||'Verification failed.');if(result.pending){show(result.message||'Payment is awaiting capture. Premium is not active yet; check /myplan shortly.',false);btn.disabled=false;btn.textContent='Check /myplan or retry';busy=false;}else{show(result.message||'Payment verified. Check your Telegram bot for activation confirmation.',true);btn.textContent='Payment submitted';}}catch(e){show(e.message+' If money was deducted, do not pay again immediately; check /myplan or contact admin.',false);btn.disabled=false;btn.textContent='Retry payment';busy=false;}},modal:{ondismiss:function(){btn.disabled=false;btn.textContent='Retry payment';busy=false;show('Checkout closed. No premium has been activated. You can retry payment.',false);}}};
const rz=new Razorpay(options);rz.on('payment.failed',function(resp){show((resp.error&&resp.error.description)||'Payment failed. No premium has been activated. Please retry.',false);btn.disabled=false;btn.textContent='Retry payment';busy=false;});rz.open();btn.textContent='Complete payment in Razorpay';
}catch(e){show(e.message,false);btn.disabled=false;btn.textContent='Retry payment';busy=false;}}
money();
</script></body></html>'''


@app.route('/')
def home():
    return "<center><h2>JARVIS Save Restricted Content Bot</h2><p>Bot service is running.</p></center>"


@app.route('/checkout/<token>')
def checkout(token):
    payload = read_checkout_token(token)
    if not payload:
        return "This payment link is invalid or expired. Please return to the bot and select the plan again.", 400
    from config import RAZORPAY_KEY_ID, PLAN_CONFIG
    if not RAZORPAY_KEY_ID:
        return "Razorpay is not configured yet. Please contact the administrator.", 503
    info = PLAN_CONFIG[payload['p']]
    return render_template_string(
        CHECKOUT_HTML, token=token, key_id=RAZORPAY_KEY_ID,
        plan_label=info['label'], duration=payload['d'].title(),
        prices={"INR": info['inr'][payload['d']], "USD": info['usd'][payload['d']]},
    )


@app.post('/api/payment/order')
def payment_order():
    body = request.get_json(silent=True) or {}
    payload = read_checkout_token(body.get('token', ''))
    if not payload:
        return jsonify(error="Payment link is invalid or expired. Go back to the bot and select the plan again."), 400
    try:
        order = create_order(payload, str(body.get('currency', 'INR')).upper())
        return jsonify(order_id=order['id'], amount=order['amount'], currency=order['currency'])
    except Exception as exc:
        print(f"Razorpay order creation failed: {exc}")
        return jsonify(error="Unable to start checkout. Please try again or contact admin."), 502


@app.post('/api/payment/verify')
def payment_verify():
    body = request.get_json(silent=True) or {}
    payload = read_checkout_token(body.get('token', ''))
    if not payload:
        return jsonify(error="Payment session expired. If money was deducted, contact admin before retrying."), 400
    try:
        ok, message = verify_checkout_payment(
            payload, body.get('razorpay_order_id', ''),
            body.get('razorpay_payment_id', ''), body.get('razorpay_signature', ''),
        )
        return jsonify(message=message, pending=not ok), (200 if ok else 202)
    except Exception as exc:
        print(f"Razorpay verification failed: {exc}")
        return jsonify(error="Payment verification could not be completed. If money was deducted, do not pay again; contact admin or check /myplan."), 400


@app.post('/razorpay/webhook')
def razorpay_webhook():
    raw = request.get_data()
    signature = request.headers.get('X-Razorpay-Signature', '')
    if not verify_webhook_signature(raw, signature):
        return jsonify(error="Invalid webhook signature"), 400
    try:
        process_webhook_event(request.get_json(force=True))
        return jsonify(status="ok"), 200
    except Exception as exc:
        print(f"Razorpay webhook processing failed: {exc}")
        return jsonify(error="Webhook processing failed"), 500


def run_web_server():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run_web_server()
