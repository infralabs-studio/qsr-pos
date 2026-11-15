from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION pos_auditevent_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'pos_auditevent is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER pos_auditevent_no_update_delete
BEFORE UPDATE OR DELETE ON pos_auditevent
FOR EACH ROW EXECUTE FUNCTION pos_auditevent_immutable();
"""

REVERSE = """
DROP TRIGGER IF EXISTS pos_auditevent_no_update_delete ON pos_auditevent;
DROP FUNCTION IF EXISTS pos_auditevent_immutable();
"""

class Migration(migrations.Migration):
    dependencies = [("pos", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]