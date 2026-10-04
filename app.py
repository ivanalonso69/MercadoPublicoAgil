import streamlit as st
import pandas as pd
import mysql.connector
import plotly.express as px
import bcrypt
import os
from dotenv import load_dotenv
from datetime import datetime

# ==============================================================================
# CONFIGURACIÓN INICIAL
# ==============================================================================
st.set_page_config(page_title="Radar Compra Ágil", page_icon="🇨🇱", layout="wide")
load_dotenv()

# ==============================================================================
# CONEXIÓN A BASE DE DATOS
# ==============================================================================
@st.cache_resource
def conectar_db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

# ==============================================================================
# SISTEMA DE AUTENTICACIÓN (LOGIN / REGISTRO)
# ==============================================================================
def hashear_password(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verificar_password(password, password_hash):
    return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))

def registrar_usuario(username, password, palabras_clave):
    conn = conectar_db()
    cursor = conn.cursor()
    try:
        hash_pw = hashear_password(password)
        cursor.execute("INSERT INTO usuarios (username, password_hash, palabras_clave) VALUES (%s, %s, %s)", 
                       (username, hash_pw, palabras_clave))
        conn.commit()
        return True
    except mysql.connector.Error:
        return False
    finally:
        cursor.close()

def login_usuario(username, password):
    conn = conectar_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
    user = cursor.fetchone()
    cursor.close()
    if user and verificar_password(password, user['password_hash']):
        return user
    return None

def actualizar_palabras_clave(username, nuevas_palabras):
    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE usuarios SET palabras_clave = %s WHERE username = %s", (nuevas_palabras, username))
    conn.commit()
    cursor.close()

# ==============================================================================
# MOTOR DE DATOS (ETL & CACHÉ)
# ==============================================================================
@st.cache_data(ttl=300) # Se actualiza cada 5 minutos
def cargar_vista_oportunidades():
    conn = conectar_db()
    query = """
        SELECT 
            c.codigo, c.nombre, c.descripcion, comp.nombre as institucion, comp.region, 
            c.direccion_entrega, c.monto_estimado, c.fecha_publicacion, c.fecha_cierre, 
            c.estado, c.link_directo 
        FROM compras_agiles c
        LEFT JOIN compradores comp ON c.rut_comprador = comp.rut
        WHERE c.estado != '8' -- Excluir las ya adjudicadas del panel principal
    """
    df = pd.read_sql(query, conn)
    df['fecha_cierre'] = pd.to_datetime(df['fecha_cierre'])
    df['fecha_publicacion'] = pd.to_datetime(df['fecha_publicacion'])
    return df

@st.cache_data(ttl=600)
def cargar_tabla_simple(tabla):
    conn = conectar_db()
    return pd.read_sql(f"SELECT * FROM {tabla}", conn)

def calcular_tiempo_restante(fecha_cierre):
    ahora = datetime.now()
    if pd.isna(fecha_cierre) or fecha_cierre < ahora:
        return "Cerrada"
    diferencia = fecha_cierre - ahora
    dias = diferencia.days
    horas = diferencia.seconds // 3600
    if dias > 0:
        return f"{dias}d {horas}h"
    return f"{horas}h {(diferencia.seconds % 3600) // 60}m"

def determinar_llamado(descripcion):
    desc = str(descripcion).lower()
    if "segundo llamado" in desc or "2do llamado" in desc or "2° llamado" in desc:
        return "2do Llamado"
    return "1er Llamado"

def calcular_match(texto, palabras_clave_usuario):
    """Calcula un % de coincidencia (1-100) basado en las palabras clave del perfil."""
    if not palabras_clave_usuario: return 0
    palabras = [p.strip().lower() for p in palabras_clave_usuario.split(",")]
    texto_evaluar = str(texto).lower()
    
    coincidencias = sum(1 for palabra in palabras if palabra in texto_evaluar)
    score = (coincidencias / len(palabras)) * 100
    return min(100, int(score))

# ==============================================================================
# INTERFAZ GRÁFICA (PANTALLAS)
# ==============================================================================

# Manejo de sesión
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
    st.session_state["user_data"] = None

if not st.session_state["logged_in"]:
    st.markdown("<h1 style='text-align: center;'>🔐 Acceso a Radar Compra Ágil</h1>", unsafe_allow_html=True)
    tab_login, tab_registro = st.tabs(["Iniciar Sesión", "Crear Cuenta"])
    
    with tab_login:
        with st.form("login_form"):
            user = st.text_input("Usuario")
            pw = st.text_input("Contraseña", type="password")
            if st.form_submit_button("Ingresar"):
                data = login_usuario(user, pw)
                if data:
                    st.session_state["logged_in"] = True
                    st.session_state["user_data"] = data
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")
                    
    with tab_registro:
        with st.form("reg_form"):
            new_user = st.text_input("Nuevo Usuario")
            new_pw = st.text_input("Contraseña", type="password")
            preferencias = st.text_area("¿Qué vendes? (Separado por comas. Ej: computadores, madera, aseo, licencias)")
            if st.form_submit_button("Registrarse"):
                if registrar_usuario(new_user, new_pw, preferencias):
                    st.success("Cuenta creada con éxito. Por favor, inicia sesión.")
                else:
                    st.error("Error: El usuario ya existe o hubo un problema.")

else:
    # --- APLICACIÓN PRINCIPAL (Usuario Logueado) ---
    usuario_actual = st.session_state["user_data"]
    
    # BARRA LATERAL
    with st.sidebar:
        st.title(f"👤 Hola, {usuario_actual['username']}")
        st.write("---")
        st.subheader("⚙️ Configuración del Algoritmo")
        mis_palabras = st.text_area("Mis Palabras Clave:", value=usuario_actual['palabras_clave'])
        if st.button("Actualizar Perfil"):
            actualizar_palabras_clave(usuario_actual['username'], mis_palabras)
            st.session_state["user_data"]['palabras_clave'] = mis_palabras
            st.success("Algoritmo actualizado")
            st.rerun()
            
        st.write("---")
        if st.button("Cerrar Sesión"):
            st.session_state["logged_in"] = False
            st.session_state["user_data"] = None
            st.rerun()

    # DASHBOARD
    st.title("📊 Centro de Inteligencia: Compra Ágil Chile")
    tab_oportunidades, tab_compradores, tab_proveedores = st.tabs(["🛒 Oportunidades Abiertas", "🏢 Análisis de Compradores", "🤝 Inteligencia Competitiva"])
    
    with tab_oportunidades:
        df_opps = cargar_vista_oportunidades()
        
        if not df_opps.empty:
            # Procesamiento de Columnas Especiales
            df_opps['Tiempo Restante'] = df_opps['fecha_cierre'].apply(calcular_tiempo_restante)
            df_opps['Llamado'] = df_opps['descripcion'].apply(determinar_llamado)
            
            # Aplicar Algoritmo de Match
            keywords = st.session_state["user_data"].get('palabras_clave', '')
            df_opps['Match (%)'] = df_opps.apply(lambda row: calcular_match(row['nombre'] + " " + row['descripcion'], keywords), axis=1)
            
            # Ordenar para mostrar lo más relevante y urgente primero
            df_opps = df_opps.sort_values(by=['Match (%)', 'fecha_cierre'], ascending=[False, True])

            # Filtros Interactivos
            col1, col2, col3 = st.columns(3)
            with col1:
                filtro_region = st.multiselect("Filtrar por Región:", df_opps['region'].dropna().unique())
            with col2:
                rango_monto = st.slider("Presupuesto Máximo ($):", 0, int(df_opps['monto_estimado'].max()), int(df_opps['monto_estimado'].max()))
            with col3:
                busqueda_libre = st.text_input("Búsqueda específica (Ej: Notebooks)")

            # Aplicar Filtros
            df_filtrado = df_opps.copy()
            if filtro_region:
                df_filtrado = df_filtrado[df_filtrado['region'].isin(filtro_region)]
            df_filtrado = df_filtrado[df_filtrado['monto_estimado'] <= rango_monto]
            if busqueda_libre:
                df_filtrado = df_filtrado[df_filtrado.apply(lambda row: row.astype(str).str.contains(busqueda_libre, case=False).any(), axis=1)]

            # Tabla Interactiva Final
            st.dataframe(
                df_filtrado[['Match (%)', 'Tiempo Restante', 'Llamado', 'nombre', 'institucion', 'monto_estimado', 'link_directo']],
                column_config={
                    "Match (%)": st.column_config.ProgressColumn("Afinidad", help="Basado en tus palabras clave", format="%d%%", min_value=0, max_value=100),
                    "monto_estimado": st.column_config.NumberColumn("Presupuesto", format="$%d"),
                    "link_directo": st.column_config.LinkColumn("Acción", display_text="Ir a Ofertar ↗️")
                },
                hide_index=True, use_container_width=True, height=500
            )
        else:
            st.info("No hay oportunidades abiertas en la base de datos en este momento.")

    with tab_compradores:
        st.subheader("🏢 Quién está comprando más en el Estado")
        df_comp = cargar_tabla_simple("compradores")
        if not df_comp.empty and not df_opps.empty:
            volumen_por_region = df_comp['region'].value_counts().reset_index()
            volumen_por_region.columns = ['Región', 'Cantidad de Entidades']
            
            col_chart1, col_chart2 = st.columns(2)
            with col_chart1:
                fig1 = px.pie(volumen_por_region, values='Cantidad de Entidades', names='Región', title="Distribución de Compradores por Región", hole=0.4)
                st.plotly_chart(fig1, use_container_width=True)
                
            with col_chart2:
                top_compradores = df_opps['institucion'].value_counts().head(10).reset_index()
                top_compradores.columns = ['Institución', 'Cotizaciones Activas']
                fig2 = px.bar(top_compradores, x='Cotizaciones Activas', y='Institución', orientation='h', title="Top 10 Entidades con más demanda actual")
                st.plotly_chart(fig2, use_container_width=True)

    with tab_proveedores:
        st.subheader("🤝 Inteligencia Competitiva: Tus Rivales")
        df_prov = cargar_tabla_simple("proveedores")
        if not df_prov.empty:
            df_prov = df_prov.sort_values(by="monto_total_ganado", ascending=False).head(50)
            
            fig3 = px.scatter(
                df_prov, x="cantidad_adjudicaciones", y="monto_total_ganado", 
                text="nombre", size="monto_total_ganado", color="monto_total_ganado",
                title="Mapa de Dominio de Proveedores (Top 50)",
                labels={"cantidad_adjudicaciones": "Veces que ha ganado", "monto_total_ganado": "Dinero Acumulado ($)"}
            )
            fig3.update_traces(textposition='top center')
            st.plotly_chart(fig3, use_container_width=True)
            
            st.dataframe(
                df_prov[['nombre', 'cantidad_adjudicaciones', 'monto_total_ganado']],
                column_config={"monto_total_ganado": st.column_config.NumberColumn("Monto Adjudicado Total", format="$%d")},
                hide_index=True, use_container_width=True
            )