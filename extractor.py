import os
import requests
import mysql.connector
from datetime import datetime
from dotenv import load_dotenv

# Cargar credenciales desde el archivo .env
load_dotenv()

def conectar_db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

def obtener_licitaciones_hoy():
    fecha_hoy = datetime.now().strftime("%d%m%Y")
    ticket = os.getenv("MP_TICKET")
    url = f"https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json?fecha={fecha_hoy}&ticket={ticket}"
    
    print(f"Consultando API de Mercado Público para la fecha {fecha_hoy}...")
    response = requests.get(url)
    
    if response.status_code == 200:
        datos = response.json()
        return datos.get('Listado', [])
    else:
        print(f"Error al consultar la API: Código {response.status_code}")
        return []

def guardar_en_base_de_datos(licitaciones):
    if not licitaciones:
        print("No hay datos para procesar.")
        return

    conexion = conectar_db()
    cursor = conexion.cursor()
    
    # Query de inserción. Si la compra ya existe, actualizamos su estado
    query = """
        INSERT INTO compras_agiles 
        (codigo, nombre_licitacion, entidad_compradora, monto_estimado, fecha_cierre, estado) 
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE estado = VALUES(estado);
    """
    
    registros_procesados = 0
    for lic in licitaciones:
        # Extraer datos asegurando que no fallen si viene vacío (usando .get())
        codigo = lic.get('CodigoExterno', 'Sin Codigo')
        nombre = lic.get('Nombre', 'Sin Nombre')[:250]
        entidad = lic.get('CodigoEstado', 'Desconocida') # Ajustar a la llave real de entidad si la API la provee directamente aquí
        estado = str(lic.get('CodigoEstado', ''))
        fecha_cierre = lic.get('FechaCierre')
        monto = 0.0 # El monto requiere un endpoint adicional, por ahora lo inicializamos en 0
        
        # Validar formato de fecha de cierre
        if fecha_cierre:
            try:
                # Convertir la cadena 'YYYY-MM-DDTHH:MM:SS' a formato compatible con MySQL
                fecha_cierre = fecha_cierre.split('T')[0] + ' ' + fecha_cierre.split('T')[1]
            except:
                fecha_cierre = None
        
        valores = (codigo, nombre, entidad, monto, fecha_cierre, estado)
        
        try:
            cursor.execute(query, valores)
            registros_procesados += 1
        except Exception as e:
            print(f"Error al insertar el código {codigo}: {e}")
            
    conexion.commit()
    cursor.close()
    conexion.close()
    print(f"¡Éxito! Se insertaron/actualizaron {registros_procesados} licitaciones en Aiven.")

if __name__ == "__main__":
    datos = obtener_licitaciones_hoy()
    guardar_en_base_de_datos(datos)