import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import mysql.connector
from datetime import datetime
from dotenv import load_dotenv
import concurrent.futures
import threading

# Cargar credenciales
load_dotenv()

# Configurar sesión robusta con reintentos automáticos para no saturar la API
def obtener_sesion_robusta():
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[ 429, 500, 502, 503, 504 ])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session

def conectar_db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

# Lock para evitar colisiones al escribir en la base de datos desde múltiples hilos
db_lock = threading.Lock()

def limpiar_cotizaciones_caducadas(cursor, conn):
    """Elimina automáticamente las cotizaciones cuya fecha de cierre ya pasó."""
    print("Iniciando limpieza de cotizaciones caducadas...")
    fecha_actual = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Gracias al ON DELETE CASCADE en MySQL, borrar aquí borra también los items asociados
    query = "DELETE FROM compras_agiles WHERE fecha_cierre < %s"
    cursor.execute(query, (fecha_actual,))
    conn.commit()
    print(f"Limpieza completada. Se eliminaron {cursor.rowcount} cotizaciones vencidas.")

def obtener_codigos_del_dia():
    """Obtiene la lista inicial de todos los códigos publicados hoy."""
    fecha_hoy = datetime.now().strftime("%d%m%Y")
    ticket = os.getenv("MP_TICKET")
    url = f"https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json?fecha={fecha_hoy}&ticket={ticket}"
    
    print(f"Obteniendo lista principal de cotizaciones para el {fecha_hoy}...")
    session = obtener_sesion_robusta()
    response = session.get(url)
    
    if response.status_code == 200:
        datos = response.json()
        licitaciones = datos.get('Listado', [])
        return [lic['CodigoExterno'] for lic in licitaciones]
    else:
        print(f"Error al conectar con Mercado Público. Código: {response.status_code}")
        return []

def procesar_detalle_licitacion(codigo):
    """Extrae el detalle profundo de una cotización específica (Items, Entidad, Montos)."""
    ticket = os.getenv("MP_TICKET")
    url = f"https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json?codigo={codigo}&ticket={ticket}"
    
    session = obtener_sesion_robusta()
    try:
        response = session.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if 'Listado' in data and len(data['Listado']) > 0:
                return data['Listado'][0]
    except Exception as e:
        print(f"Timeout o error extrayendo código {codigo}: {e}")
    return None

def formatear_fecha(cadena_fecha):
    if not cadena_fecha:
        return None
    try:
        return cadena_fecha.split('T')[0] + ' ' + cadena_fecha.split('T')[1][:8]
    except:
        return None

def guardar_licitacion_e_items(detalle, cursor, conn):
    """Guarda la información de la licitación y sus productos."""
    if not detalle: return
    
    codigo = detalle.get('CodigoExterno')
    nombre = detalle.get('Nombre', '')[:250]
    estado = detalle.get('CodigoEstado', '')
    
    # Datos de la entidad compradora
    comprador = detalle.get('Comprador', {})
    entidad_rut = comprador.get('RutUnidad', 'S/N')
    entidad_nombre = comprador.get('NombreOrganismo', 'Desconocido')[:150]
    region = comprador.get('RegionUnidad', '')[:50]
    
    # Fechas
    fechas = detalle.get('Fechas', {})
    fecha_creacion = formatear_fecha(fechas.get('FechaCreacion'))
    fecha_cierre = formatear_fecha(fechas.get('FechaCierre'))
    
    dias_publicacion = datetime.now().strftime("%A") # Día de la semana
    
    # Monto total estimado
    monto_estimado = detalle.get('MontoEstimado', 0.0)
    if not isinstance(monto_estimado, (int, float)):
        monto_estimado = 0.0
        
    query_lic = """
        INSERT INTO compras_agiles 
        (codigo, nombre_licitacion, entidad_rut, entidad_compradora, region, monto_estimado, fecha_creacion, fecha_cierre, estado, dias_publicacion) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE 
        monto_estimado = VALUES(monto_estimado),
        fecha_cierre = VALUES(fecha_cierre),
        estado = VALUES(estado);
    """
    
    # Datos de los items (productos)
    items = detalle.get('Items', {}).get('Cantidad', 0)
    lista_items = detalle.get('Items', {}).get('Listado', [])
    
    query_item = """
        INSERT INTO compra_agil_items 
        (codigo_licitacion, producto, categoria, cantidad, unidad_medida) 
        VALUES (%s, %s, %s, %s, %s)
    """

    # Guardar ganadores si ya está adjudicada (Estado 8 = Adjudicada en MP)
    adjudicacion = detalle.get('Adjudicacion')
    query_hist = None
    valores_hist = None
    if adjudicacion and detalle.get('CodigoEstado') == 8:
        # A veces puede haber múltiples proveedores ganadores en diferentes items, simplificamos al principal de la cabecera
        proveedor = adjudicacion.get('Oferente', {})
        rut_prov = proveedor.get('RutProveedor')
        nom_prov = proveedor.get('NombreProveedor')
        fecha_adj = formatear_fecha(adjudicacion.get('Fecha'))
        monto_adj = adjudicacion.get('MontoTotal', 0) # Monto puede variar según moneda, requiere limpieza avanzada a futuro
        
        query_hist = """
            INSERT IGNORE INTO historico_adjudicaciones 
            (codigo_licitacion, nombre_licitacion, entidad_compradora, rut_proveedor_ganador, nombre_proveedor_ganador, monto_adjudicado, fecha_adjudicacion)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        valores_hist = (codigo, nombre, entidad_nombre, rut_prov, nom_prov, monto_adj, fecha_adj)

    # Iniciar escritura asegurada en base de datos
    with db_lock:
        try:
            cursor.execute(query_lic, (codigo, nombre, entidad_rut, entidad_nombre, region, monto_estimado, fecha_creacion, fecha_cierre, estado, dias_publicacion))
            
            # Limpiar items anteriores para evitar duplicados en el UPDATE
            cursor.execute("DELETE FROM compra_agil_items WHERE codigo_licitacion = %s", (codigo,))
            
            for itm in lista_items:
                val_item = (
                    codigo, 
                    str(itm.get('NombreProducto', ''))[:250], 
                    str(itm.get('Categoria', ''))[:150],
                    itm.get('Cantidad', 0),
                    str(itm.get('UnidadMedida', ''))[:50]
                )
                cursor.execute(query_item, val_item)
                
            if query_hist and valores_hist:
                cursor.execute(query_hist, valores_hist)
                
            conn.commit()
            print(f"✓ Procesado: {codigo} - {entidad_nombre[:30]}...")
        except Exception as e:
            print(f"Error escribiendo en BD el código {codigo}: {e}")
            conn.rollback()

def ejecutar_extraccion_masiva():
    conn = conectar_db()
    cursor = conn.cursor()
    
    # 1. Limpiar cotizaciones vencidas
    limpiar_cotizaciones_caducadas(cursor, conn)
    
    # 2. Extraer códigos nuevos
    codigos = obtener_codigos_del_dia()
    
    if not codigos:
        print("No se encontraron códigos para extraer hoy.")
        cursor.close()
        conn.close()
        return

    print(f"Se extraerá el detalle exhaustivo de {len(codigos)} cotizaciones. Esto puede tomar unos minutos...")
    
    # 3. Multithreading para extraer datos rápido. 
    # MAX_WORKERS = 5 para no saturar ni ser bloqueado por la API de Mercado Público
    procesados = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        # Ejecutar peticiones en paralelo
        futuros = {executor.submit(procesar_detalle_licitacion, cod): cod for cod in codigos}
        
        for futuro in concurrent.futures.as_completed(futuros):
            detalle = futuro.result()
            if detalle:
                guardar_licitacion_e_items(detalle, cursor, conn)
                procesados += 1

    cursor.close()
    conn.close()
    print(f"=== EXTRACCIÓN FINALIZADA. {procesados} cotizaciones actualizadas con éxito. ===")

if __name__ == "__main__":
    ejecutar_extraccion_masiva()