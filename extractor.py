# ==============================================================================
# PROYECTO: RADAR COMPRA ÁGIL CHILE - EXTRACTOR MANUAL DE PRECISIÓN
# DESCRIPCIÓN: Sistema robusto de extracción bajo demanda. Extrae cotizaciones,
#              ítems, proveedores, entidades compradoras y logística.
# ==============================================================================

import os
import sys
import time
import logging
import threading
import concurrent.futures
import datetime  # Importación absoluta para evitar cualquier error de 'not defined'
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# 1. CONFIGURACIÓN INICIAL Y LOGS
# ------------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class MonitorRendimiento:
    """Rastrea el éxito y fracaso de las inserciones en la base de datos."""
    def __init__(self):
        self.tiempo_inicio = time.time()
        self.exitos = 0
        self.errores = 0
        self.lock = threading.Lock()
        
    def sumar_exito(self):
        with self.lock:
            self.exitos += 1
            
    def sumar_error(self):
        with self.lock:
            self.errores += 1

# ------------------------------------------------------------------------------
# 2. CLASE EXTRACTORA PRINCIPAL
# ------------------------------------------------------------------------------
class ExtractorManualCompraAgil:
    def __init__(self):
        self.ticket = os.getenv("MP_TICKET")
        if not self.ticket:
            logging.error("No se encontró MP_TICKET en el archivo .env")
            sys.exit(1)
            
        self.db_host = os.getenv("DB_HOST")
        self.db_port = os.getenv("DB_PORT")
        self.db_user = os.getenv("DB_USER")
        self.db_password = os.getenv("DB_PASSWORD")
        self.db_name = os.getenv("DB_NAME")
        
        self.monitor = MonitorRendimiento()
        self.sesion_http = self._inicializar_red()

    def _inicializar_red(self):
        """Prepara una sesión resistente a los bloqueos de Mercado Público."""
        sesion = requests.Session()
        # Reintentos automáticos si el gobierno arroja error 500
        estrategia = Retry(
            total=5, 
            backoff_factor=1, 
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adaptador = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=estrategia)
        sesion.mount('https://', adaptador)
        sesion.mount('http://', adaptador)
        return sesion

    def conectar_mysql(self):
        """Genera una conexión independiente a la BD. Crítico para evitar choques entre hilos."""
        try:
            return mysql.connector.connect(
                host=self.db_host,
                port=self.db_port,
                user=self.db_user,
                password=self.db_password,
                database=self.db_name
            )
        except Error as e:
            logging.error(f"Error conectando a Aiven MySQL: {e}")
            return None

    # --------------------------------------------------------------------------
    # 3. FUNCIONES DE LIMPIEZA Y FORMATEO DE DATOS
    # --------------------------------------------------------------------------
    
    def limpiar_texto(self, texto, max_length):
        """Asegura que los textos no superen el límite de las columnas en MySQL."""
        if texto is None:
            return ""
        texto_str = str(texto).strip()
        # Reemplazar saltos de línea extraños que puedan romper la tabla
        texto_str = texto_str.replace('\r', ' ').replace('\n', ' ')
        return texto_str[:max_length]

    def extraer_fecha(self, fecha_cruda):
        """Convierte las fechas ISO de Mercado Público a formato estándar SQL."""
        if not fecha_cruda: 
            return None
        try:
            # Transforma "2026-10-04T12:00:00" a "2026-10-04 12:00:00"
            return fecha_cruda.replace('T', ' ')[:19]
        except Exception:
            return None

    def extraer_monto(self, monto_crudo):
        """Asegura que el presupuesto estimado sea siempre un valor numérico seguro."""
        if isinstance(monto_crudo, (int, float)):
            return float(monto_crudo)
        try:
            return float(str(monto_crudo).replace(',', '.'))
        except (ValueError, TypeError):
            return 0.0

    # --------------------------------------------------------------------------
    # 4. MÓDULOS DE INSERCIÓN SQL AISLADOS
    # --------------------------------------------------------------------------
    
    def registrar_comprador(self, cursor, datos_comprador):
        """Almacena la institución pública que genera la cotización."""
        rut = self.limpiar_texto(datos_comprador.get('RutUnidad', 'Sin RUT'), 20)
        nombre = self.limpiar_texto(datos_comprador.get('NombreOrganismo', 'Entidad Desconocida'), 150)
        region = self.limpiar_texto(datos_comprador.get('RegionUnidad', 'Región no especificada'), 50)
        
        sql = """
            INSERT IGNORE INTO compradores (rut, nombre, region) 
            VALUES (%s, %s, %s)
        """
        cursor.execute(sql, (rut, nombre, region))
        return rut

    def registrar_proveedor_y_adjudicacion(self, cursor, adjudicacion, codigo_licitacion, estado):
        """Si la licitación cerró y tiene ganador, extrae sus datos y alimenta el historial."""
        # Estado 8 = Adjudicada
        if not adjudicacion or estado != '8':
            return
            
        oferente = adjudicacion.get('Oferente', {})
        rut_proveedor = self.limpiar_texto(oferente.get('RutProveedor', ''), 20)
        nombre_proveedor = self.limpiar_texto(oferente.get('NombreProveedor', ''), 200)
        monto_ganado = self.extraer_monto(adjudicacion.get('MontoTotal', 0))
        fecha_victoria = self.extraer_fecha(adjudicacion.get('Fecha'))
        
        if not rut_proveedor:
            return
            
        # 1. Actualizar la tabla de Proveedores (Suma montos y cantidad de victorias)
        sql_proveedor = """
            INSERT INTO proveedores (rut, nombre, cantidad_adjudicaciones, monto_total_ganado) 
            VALUES (%s, %s, 1, %s)
            ON DUPLICATE KEY UPDATE 
            cantidad_adjudicaciones = cantidad_adjudicaciones + 1,
            monto_total_ganado = monto_total_ganado + %s,
            nombre = VALUES(nombre)
        """
        cursor.execute(sql_proveedor, (rut_proveedor, nombre_proveedor, monto_ganado, monto_ganado))
        
        # 2. Registrar la victoria exacta en el historial de adjudicaciones
        sql_historial = """
            INSERT IGNORE INTO historico_adjudicaciones 
            (codigo_licitacion, rut_proveedor, monto_adjudicado, fecha_adjudicacion) 
            VALUES (%s, %s, %s, %s)
        """
        cursor.execute(sql_historial, (codigo_licitacion, rut_proveedor, monto_ganado, fecha_victoria))

    def registrar_items_solicitados(self, cursor, lista_items, codigo_licitacion):
        """Extrae la lista de productos específicos que componen la cotización."""
        # Limpiar registros antiguos para evitar duplicación al actualizar
        cursor.execute("DELETE FROM compra_agil_items WHERE codigo_licitacion = %s", (codigo_licitacion,))
        
        sql_item = """
            INSERT INTO compra_agil_items (codigo_licitacion, producto, cantidad, unidad_medida) 
            VALUES (%s, %s, %s, %s)
        """
        for item in lista_items:
            producto = self.limpiar_texto(item.get('NombreProducto', 'Producto sin nombre'), 255)
            cantidad = self.extraer_monto(item.get('Cantidad', 0))
            unidad = self.limpiar_texto(item.get('UnidadMedida', 'Unidad'), 50)
            
            cursor.execute(sql_item, (codigo_licitacion, producto, cantidad, unidad))

    def registrar_compra_agil_principal(self, cursor, detalle, rut_comprador):
        """Inserta la ficha completa de la cotización con fechas, links y logística."""
        codigo = self.limpiar_texto(detalle.get('CodigoExterno', ''), 50)
        nombre = self.limpiar_texto(detalle.get('Nombre', ''), 255)
        descripcion = self.limpiar_texto(detalle.get('Descripcion', ''), 65000)
        estado = self.limpiar_texto(detalle.get('CodigoEstado', ''), 50)
        
        # Logística y Presupuesto
        direccion = self.limpiar_texto(detalle.get('DireccionEntrega', 'No informada'), 255)
        plazo = self.limpiar_texto(detalle.get('DiasEntrega', 'No especificado'), 100)
        presupuesto = self.extraer_monto(detalle.get('MontoEstimado', 0))
        
        # Fechas
        fechas = detalle.get('Fechas', {})
        fecha_pub = self.extraer_fecha(fechas.get('FechaPublicacion'))
        fecha_cierre = self.extraer_fecha(fechas.get('FechaCierre'))
        
        # Enlace oficial
        link = f"https://www.mercadopublico.cl/Portal/Modules/Site/Busquedas/BuscadorAvanzado.aspx?qs={codigo}"
        
        sql_licitacion = """
            INSERT INTO compras_agiles 
            (codigo, nombre, descripcion, rut_comprador, direccion_entrega, 
             plazo_entrega, monto_estimado, fecha_publicacion, fecha_cierre, estado, link_directo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
            fecha_cierre = VALUES(fecha_cierre), 
            estado = VALUES(estado),
            direccion_entrega = VALUES(direccion_entrega),
            plazo_entrega = VALUES(plazo_entrega)
        """
        cursor.execute(sql_licitacion, (
            codigo, nombre, descripcion, rut_comprador, direccion, 
            plazo, presupuesto, fecha_pub, fecha_cierre, estado, link
        ))
        
        return codigo, estado

    # --------------------------------------------------------------------------
    # 5. WORKLOAD PRINCIPAL (DESCARGA Y PROCESAMIENTO)
    # --------------------------------------------------------------------------
    
    def procesar_codigo_individual(self, codigo):
        """Descarga el JSON profundo de una cotización y delega su almacenamiento."""
        url = f"https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json?codigo={codigo}&ticket={self.ticket}"
        conexion = None
        cursor = None
        
        try:
            # 1. Obtener JSON desde Mercado Público
            respuesta = self.sesion_http.get(url, timeout=15)
            if respuesta.status_code != 200:
                self.monitor.sumar_error()
                return
                
            datos_json = respuesta.json()
            listado = datos_json.get('Listado', [])
            if not listado:
                self.monitor.sumar_error()
                return
                
            detalle = listado[0]
            
            # 2. Abrir conexión a base de datos aislada para este hilo
            conexion = self.conectar_mysql()
            if not conexion:
                self.monitor.sumar_error()
                return
            cursor = conexion.cursor()
            
            # 3. Ejecutar las inserciones en orden relacional
            rut_comprador = self.registrar_comprador(cursor, detalle.get('Comprador', {}))
            codigo_bd, estado = self.registrar_compra_agil_principal(cursor, detalle, rut_comprador)
            self.registrar_items_solicitados(cursor, detalle.get('Items', {}).get('Listado', []), codigo_bd)
            self.registrar_proveedor_y_adjudicacion(cursor, detalle.get('Adjudicacion'), codigo_bd, estado)
            
            # 4. Confirmar transacción
            conexion.commit()
            self.monitor.sumar_exito()
            logging.info(f"[OK] Extraído e ingresado: {codigo}")
            
        except Exception as error_general:
            if conexion: 
                conexion.rollback()
            self.monitor.sumar_error()
            logging.debug(f"Fallo aislando el código {codigo}: {error_general}")
            
        finally:
            if cursor: cursor.close()
            if conexion: conexion.close()

    def recolectar_lista_maestra(self):
        """Busca todas las cotizaciones abiertas o publicadas recientemente."""
        codigos_encontrados = set()
        
        logging.info("Buscando cotizaciones en Mercado Público...")
        hoy = datetime.datetime.now()  # Uso directo del módulo datetime seguro
        
        # Buscamos en una ventana de los últimos 3 días para garantizar captura
        for i in range(3):
            # Restar días usando datetime.timedelta de forma segura y directa
            fecha_busqueda = hoy - datetime.timedelta(days=i)
            fecha_str = fecha_busqueda.strftime("%d%m%Y")
            
            url = f"https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json?fecha={fecha_str}&ticket={self.ticket}"
            logging.info(f"Escaneando publicaciones del día: {fecha_str}")
            
            try:
                respuesta = self.sesion_http.get(url, timeout=20)
                if respuesta.status_code == 200:
                    licitaciones = respuesta.json().get('Listado', [])
                    for lic in licitaciones:
                        if 'CodigoExterno' in lic:
                            codigos_encontrados.add(lic['CodigoExterno'])
            except Exception as error_red:
                logging.warning(f"No se pudo consultar la fecha {fecha_str}: {error_red}")
                
        lista_final = list(codigos_encontrados)
        logging.info(f"Total de oportunidades únicas descubiertas: {len(lista_final)}")
        return lista_final

    # --------------------------------------------------------------------------
    # 6. ORQUESTADOR Y LANZADOR
    # --------------------------------------------------------------------------
    
    def ejecutar_extraccion_manual(self):
        """Inicia el proceso completo de extracción a petición del usuario."""
        logging.info("======================================================")
        logging.info("🔧 INICIANDO EXTRACCIÓN MANUAL DE DATOS (MERCADO PÚBLICO)")
        logging.info("======================================================")
        
        lista_codigos = self.recolectar_lista_maestra()
        
        if not lista_codigos:
            logging.info("No se encontraron cotizaciones para procesar hoy.")
            return
            
        logging.info(f"Iniciando descarga en paralelo (10 Hilos de trabajo)...")
        
        # Pool de hilos para descargar múltiples cotizaciones a la vez
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # Enviamos todos los códigos al pool de trabajo
            futuros = {executor.submit(self.procesar_codigo_individual, cod): cod for cod in lista_codigos}
            
            for futuro in concurrent.futures.as_completed(futuros):
                pass # El log de éxito se maneja dentro de procesar_codigo_individual
                
        tiempo_total = round(time.time() - self.monitor.tiempo_inicio, 2)
        
        logging.info("======================================================")
        logging.info("✅ EXTRACCIÓN Y ALMACENAMIENTO COMPLETADOS")
        logging.info(f"⏱️ Tiempo transcurrido : {tiempo_total} segundos")
        logging.info(f"📥 Registros guardados : {self.monitor.exitos}")
        logging.info(f"⚠️ Errores o descartes : {self.monitor.errores}")
        logging.info("======================================================")

# ==============================================================================
# INICIO DEL SCRIPT
# ==============================================================================
if __name__ == "__main__":
    app_extractor = ExtractorManualCompraAgil()
    app_extractor.ejecutar_extraccion_manual()