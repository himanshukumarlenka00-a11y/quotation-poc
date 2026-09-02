.mode list
.separator ' | '
SELECT '=== TOTAL ===', COUNT(*) FROM master_products;
SELECT '';
SELECT '=== COMPLETENESS (rows missing each field) ===','';
SELECT 'no price (all tiers 0/null)', COUNT(*) FROM master_products WHERE COALESCE(price_3star,0)=0 AND COALESCE(price_4star,0)=0 AND COALESCE(price_3star_usd,0)=0 AND COALESCE(price_4star_usd,0)=0;
SELECT 'no HSN code', COUNT(*) FROM master_products WHERE COALESCE(hsn_code,'')='';
SELECT 'GST = 0 or null', COUNT(*) FROM master_products WHERE COALESCE(gst_pct,0)=0;
SELECT 'no cost (margin blind)', COUNT(*) FROM master_products WHERE COALESCE(cost,0)=0;
SELECT 'no image', COUNT(*) FROM master_products WHERE COALESCE(image_path,'')='';
SELECT 'no brand', COUNT(*) FROM master_products WHERE COALESCE(brand,'')='';
SELECT 'no model code', COUNT(*) FROM master_products WHERE COALESCE(original_model,'')='';
SELECT '';
SELECT '=== DATA HYGIENE ===','';
SELECT 'dirty name (newline / double-space)', COUNT(*) FROM master_products WHERE product LIKE '%'||char(10)||'%' OR product LIKE '%  %';
SELECT 'dup-name groups within a catalogue', COUNT(*) FROM (SELECT file_name, LOWER(TRIM(product)) p, COUNT(*) c FROM master_products WHERE COALESCE(product,'')<>'' GROUP BY file_name, LOWER(TRIM(product)) HAVING c>1);
SELECT 'rows inside those dup groups', COALESCE(SUM(c),0) FROM (SELECT COUNT(*) c FROM master_products WHERE COALESCE(product,'')<>'' GROUP BY file_name, LOWER(TRIM(product)) HAVING c>1);
SELECT '';
SELECT '=== GST=0 by catalogue (top) ===','';
SELECT file_name, COUNT(*) FROM master_products WHERE COALESCE(gst_pct,0)=0 GROUP BY file_name ORDER BY COUNT(*) DESC LIMIT 8;
SELECT '';
SELECT '=== no-image by catalogue (top) ===','';
SELECT file_name, COUNT(*) FROM master_products WHERE COALESCE(image_path,'')='' GROUP BY file_name ORDER BY COUNT(*) DESC LIMIT 8;
