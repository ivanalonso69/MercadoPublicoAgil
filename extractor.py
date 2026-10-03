import os
import sys
import time
import logging
import threading
import concurrent.futures
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# 1. CONFIGURACIÓN DE LOGS Y ENTORNO
# ------------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(threadName)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# ------------------------------------------------------------------------------
# 2. CLASE PRINCIPAL DEL MOTOR DE EXTRACCIÓN
# ------------------------------------------------------------------------------
class MercadoPublicoExtractor:
    def __init__(self):
        self.ticket = os.getenv("MP_TICKET")
        self.db_host = os.getenv("DB_HOST")
        self.db_port = os.getenv("DB_PORT")
        self.db_user = os.getenv("DB_USER")
        self.db_password = os.getenv("DB_PASSWORD")
        self.db_name = os.getenv("DB_NAME")
        
        if not self.ticket:
            logging.error("¡FATAL! No se encontró el MP_TICKET en el archivo .env")
            sys.exit(1)
            
        self.db_lock = threading.Lock()
        self.session = self._crear_sesion_robusta()

    def _crear_sesion_robusta(self):
        """Crea una sesión HTTP con políticas agresivas de reintento para máxima estabilidad."""
        session = requests.Session()
        retries = Retry(
            total=8,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=retries)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        return session

    def conectar_db(self):
        """Establece conexión directa con la base de datos MySQL en Aiven."""
        try:
            conn = mysql.connector.connect(
                host=self.db_host,
                port=self.db_port,
                user=self.db_user,
                password=self.db_password,
                database=self.db_name,
                autocommit=False
            )
            return conn
        except Error as e:
            logging.error(f"Error al conectar con MySQL en Aiven: {e}")
            return None

    def limpiar_caducadas(self):
        """Elimina automáticamente todas las cotizaciones cuya fecha de cierre ya expiró."""
        logging.info("Iniciando barrido de limpieza para cotizaciones caducadas...")
        conn = self.conectar_db()
        if not conn: return
        
        try:
            cursor = conn.cursor()
            fecha_actual = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            query = "DELETE FROM compras_agiles WHERE fecha_cierre < %s"
            cursor.execute(query, (fecha_actual,))
            conn.commit()
            
            filas_eliminadas = cursor.rowcount
            logging.info(f"Limpieza finalizada con éxito. Se eliminaron {filas_eliminadas} registros vencidos.")
        except Error as e:
            logging.error(f"Error durante la limpieza de caducadas: {e}")
            conn.rollback()
        finally:
            cursor.close()
            conn.close()

    def recolectar_codigos_masivos(self):
        """
        Recorre un rango dinámico de los últimos 20 días para garantizar
        la recopilación de todas las órdenes de compra ágil activas a nivel país.
        """
        codigos_encontrados = set()
        hoy = datetime.now()
        rango_dias = 20
        
        logging.info(f"Iniciando escaneo de códigos activos en un rango de {rango_dias} días...")
        
        for i in range(rango_dias):
            fecha_iter = hoy - timedelta(days=i)
            fecha_str = fecha_iter.strftime("%d%m%Y")
            url = f"https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json?fecha={fecha_str}&ticket={self.ticket}"
            
            try:
                response = self.session.get(url, timeout=20)
                if response.status_code == 200:
                    data = response.json()
                    listado = data.get('Listado', [])
                    for lic in listado:
                        codigo = lic.get('CodigoExterno')
                        if codigo:
                            codigos_encontrados.add(codigo)
            except Exception as e:
                logging.warning(f"Excepción consultando fecha {fecha_str}: {e}")
                
        logging.info(f"Total de códigos únicos identificados para análisis: {len(codigos_encontrados)}")
        return list(codigos_encontrados)

    def extraer_detalle_unitario(self, codigo):
        """Consulta el detalle exhaustivo de una licitación específica en la API."""
        url = f"https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json?codigo={codigo}&ticket={self.ticket}"
        try:
            response = self.session.get(url, timeout=12)
            if response.status_code == 200:
                data = response.json()
                listado = data.get('Listado', [])
                if listado and len(listado) > 0:
                    return listado[0]
        except Exception as e:
            logging.debug(f"Error de red al extraer detalle de {codigo}: {e}")
        return None

    def _formatear_fecha(self, raw_date):
        if not raw_date: return None
        try:
            return raw_date.split('T')[0] + ' ' + raw_date.split('T')[1][:8]
        except:
            return None

    def calcular_tiempo_restante(self, fecha_cierre_str):
        """Calcula de forma exacta el tiempo restante para el cierre de la cotización."""
        if not fecha_cierre_str: return "Sin definir"
        try:
            cierre = datetime.strptime(fecha_cierre_str, '%Y-%m-%d %H:%M:%S')
            ahora = datetime.now()
            if cierre <= ahora:
                return "Caducada"
            dif = cierre - ahora
            dias = dif.days
            horas = dif.seconds // 3600
            minutos = (dif.seconds % 3600) // 60
            if dias > 0:
                return f"{dias} días, {horas} hrs"
            else:
                return f"{horas} hrs, {minutos} mins"
        except:
            return "Indefinido"

    def persistir_en_base_datos(self, detalle, cursor, conn):
        """Inserta o actualiza la licitación, sus productos y el historial de competencia."""
        if not detalle: return
        
        codigo = detalle.get('CodigoExterno')
        nombre = str(detalle.get('Nombre', ''))[:250]
        estado = str(detalle.get('CodigoEstado', ''))
        
        comprador = detalle.get('Comprador', {})
        entidad_rut = str(comprador.get('RutUnidad', 'S/N'))[:20]
        entidad_nombre = str(comprador.get('NombreOrganismo', 'Desconocido'))[:150]
        region = str(comprador.get('RegionUnidad', ''))[:50]
        
        fechas = detalle.get('Fechas', {})
        fecha_creacion = self._formatear_fecha(fechas.get('FechaCreacion'))
        fecha_cierre = self._formatear_fecha(fechas.get('FechaCierre'))
        
        # Identificar el día de la semana en que fue publicada o registrada
        dias_publicacion = datetime.now().strftime("%A")
        
        monto_estimado = detalle.get('MontoEstimado', 0.0)
        if not isinstance(monto_estimado, (int, float)):
            monto_estimado = 0.0
            
        query_licitacion = """
            INSERT INTO compras_agiles 
            (codigo, nombre_licitacion, entidad_rut, entidad_compradora, region, monto_estimado, fecha_creacion, fecha_cierre, estado, dias_publicacion) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
            monto_estimado = VALUES(monto_estimado),
            fecha_cierre = VALUES(fecha_cierre),
            estado = VALUES(estado);
        """
        
        items = detalle.get('Items', {}).get('Listado', [])
        query_item = """
            INSERT INTO compra_agil_items 
            (codigo_licitacion, producto, categoria, cantidad, unidad_medida) 
            VALUES (%s, %s, %s, %s, %s)
        """

        # Inteligencia de competencia e histórico de ganadores
        adjudicacion = detalle.get('Adjudicacion')
        query_hist = None
        valores_hist = None
        if adjudicacion and detalle.get('CodigoEstado') == 8:
            proveedor = adjudicacion.get('Oferente', {})
            rut_prov = str(proveedor.get('RutProveedor', ''))[:20]
            nom_prov = str(proveedor.get('NombreProveedor', ''))[:200]
            fecha_adj = self._formatear_fecha(adjudicacion.get('Fecha'))
            monto_adj = adjudicacion.get('MontoTotal', 0)
            
            query_hist = """
                INSERT IGNORE INTO historico_adjudicaciones 
                (codigo_licitacion, nombre_licitacion, entidad_compradora, rut_proveedor_ganador, nombre_proveedor_ganador, monto_adjudicado, fecha_adjudicacion)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            valores_hist = (codigo, nombre, entidad_nombre, rut_prov, nom_prov, monto_adj, fecha_adj)

        with self.db_lock:
            try:
                cursor.execute(query_licitacion, (
                    codigo, nombre, entidad_rut, entidad_nombre, region, 
                    monto_estimado, fecha_creacion, fecha_cierre, estado, dias_publicacion
                ))
                
                # Actualizar productos relacionados limpiando versiones previas
                cursor.execute("DELETE FROM compra_agil_items WHERE codigo_licitacion = %s", (codigo,))
                for itm in items:
                    cursor.execute(query_item, (
                        codigo,
                        str(itm.get('NombreProducto', ''))[:250],
                        str(itm.get('Categoria', ''))[:150],
                        itm.get('Cantidad', 0),
                        str(itm.get('UnidadMedida', ''))[:50]
                    ))
                    
                if query_hist and valores_hist:
                    cursor.execute(query_hist, valores_hist)
                    
                conn.commit()
                tiempo_restante = self.calcular_tiempo_restante(fecha_cierre)
                logging.info(f"Sincronizado con éxito -> Código: {codigo} | Entidad: {entidad_nombre[:25]} | Cierre: {tiempo_restante}")
            except Error as e:
                logging.error(f"Error escribiendo en BD el registro {codigo}: {e}")
                conn.rollback()

    def ejecutar_proceso_completo(self):
        """Orquesta todo el flujo de limpieza, descarga multihilo y persistencia masiva."""
        tiempo_inicio = time.time()
        logging.info("==================================================")
        logging.info("INICIO DE EXTRACCIÓN MASIVA - MERCADO PÚBLICO CHILE")
        logging.info("==================================================")
        
        # Paso 1: Limpiar registros caducados
        self.limpiar_caducadas()
        
        # Paso 2: Recolectar códigos abiertos/activos
        codigos = self.recolectar_codigos_masivos()
        if not codigos:
            logging.warning("No se obtuvieron códigos para procesar en este ciclo.")
            return

        conn = self.conectar_db()
        if not conn: return
        cursor = conn.cursor()

        logging.info(f"Iniciando procesamiento concurrente de {len(codigos)} cotizaciones...")
        
        # Paso 3: Motor multihilo de alto rendimiento (12 workers simultáneos)
        procesados = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            futuros = {executor.submit(self.extraer_detalle_unitario, cod): cod for cod in codigos}
            
            for futuro in concurrent.futures.as_completed(futuros):
                detalle = futuro.result()
                if detalle:
                    # Validar estrictamente que la cotización siga vigente
                    fechas = detalle.get('Fechas', {})
                    cierre_str = self._formatear_fecha(fechas.get('FechaCierre'))
                    if cierre_str:
                        try:
                            cierre_dt = datetime.strptime(cierre_str, '%Y-%m-%d %H:%M:%S')
                            if cierre_dt > datetime.now():
                                self.persistir_en_base_datos(detalle, cursor, conn)
                                procesados += 1
                        except:
                            pass

        cursor.close()
        conn.close()
        
        tiempo_total = round(time.time() - tiempo_inicio, 2)
        logging.info("==================================================")
        logging.info(f"EXTRACCIÓN GLOBAL COMPLETADA EXITOSAMENTE.")
        logging.info(f"Total de oportunidades vigentes procesadas: {procesados}")
        logging.info(f"Tiempo total de ejecución: {tiempo_total} segundos.")
        logging.info("==================================================")

if __name__ == "__main__":
    extractor = MercadoPublicoExtractor()
    extractor.ejecutar_proceso_completo()