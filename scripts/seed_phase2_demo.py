"""Create the canonical 7-line carton RFQ (demo scaffolding, no AI) and seed responses."""
import sys, os; sys.path.insert(0, ".")
from rfq_copilot.config import Settings
from rfq_copilot.persistence import RFQRepository
from rfq_copilot.ai_service import get_ai_service
from rfq_copilot.supplier_service import SupplierService
from rfq_copilot.schema import RFQ, LineItem, SpecAttr, Question, Section, Importance, RFQStatus, FieldStatus, Source
from rfq_copilot.fields import new_field_set

SIZES=['10 x 10 x 5','12 x 10 x 6','15 x 10 x 8','18 x 12 x 10','20 x 15 x 10','24 x 18 x 12','30 x 20 x 15']
s=Settings.from_env(); repo=RFQRepository(s.db_path)
rfq = RFQ(id='rfq_phase2_demo', title='Corrugated Carton Boxes — 7 sizes', product='Corrugated Carton Boxes',
          category='Packaging', product_type='Corrugated shipping cartons', fields=new_field_set(),
          status=RFQStatus.SUPPLIER_READY, turn=4)
for i,sz in enumerate(SIZES,1):
    rfq.line_items.append(LineItem(id='LINE-%03d'%i, product='Corrugated carton',
        specifications=[SpecAttr('Dimensions', sz, 'in')], quantity=2000, unit='pcs',
        source=Source.BUYER_EXPLICIT, evidence=sz))
rfq.line_seq = 7
for key, val in [('destination','Mumbai, India'),('currency','USD'),('customization_type','Custom made-to-spec'),
                 ('board_grade','3-ply B flute'),('printing','2 colour flexo')]:
    fv = rfq.fields.get(key)
    if fv is None:
        from rfq_copilot.schema import FieldValue
        fv = FieldValue(key=key, label=key.replace('_',' ').title(), section=Section.TECHNICAL); rfq.fields[key]=fv
    fv.value, fv.status, fv.source, fv.evidence = val, FieldStatus.PROVIDED, Source.BUYER_EXPLICIT, val
rfq.questions=[
 Question(id='q_cert', category=Section.QUALITY, question='Which certifications can you provide (FSC, ISO 9001)?', field_key='certifications', importance=Importance.RECOMMENDED),
 Question(id='q_pay', category=Section.COMMERCIAL, question='What payment terms do you offer?', field_key='payment_terms', importance=Importance.RECOMMENDED),
 Question(id='q_lead', category=Section.LOGISTICS, question='What is your production lead time?', field_key='required_delivery_date', importance=Importance.REQUIRED),
 Question(id='q_print', category=Section.TECHNICAL, question='Is 2-colour printing included in your price?', field_key='printing', importance=Importance.RECOMMENDED),
 Question(id='q_moq', category=Section.COMMERCIAL, question='What is your minimum order quantity per size?', field_key='quantity', importance=Importance.RECOMMENDED),
]
# Without this the readiness score stays at its default 0, so the saved-RFQ list showed
# this fixture as "Supplier-ready · 0% complete" — two statements that contradict each
# other. The score is computed from the fields above rather than asserted.
from rfq_copilot.guards import compute_completeness
rfq.completeness = compute_completeness(rfq, None, rfq.turn)
repo.save_rfq(rfq)
svc = SupplierService(repo, get_ai_service(s), s)
created = svc.seed_demo_responses(rfq.id)
print('RFQ %s with %d lines' % (rfq.id, len(rfq.line_items)))
for r in created:
    sup = svc.supplier(r.supplier_id)
    b = svc.bundle(r.id)
    print('  %-24s %-18s %s' % (sup.name, r.response_type.value, ', '.join(d.filename for d in b.documents)))
print('suppliers on file:', [s_.name for s_ in svc.store.list_suppliers()])
