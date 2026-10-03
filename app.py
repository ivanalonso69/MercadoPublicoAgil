import streamlit as st
import pandas as pd
import mysql.connector
import plotly.express as px
import plotly.graph_objects as go
import bcrypt
import os
from dotenv import load_dotenv

# ==========================================
# CONFIGURACIÓN INICIAL DE LA PÁGINA
# ==========================================
st.set_page_config(
    page_title="Radar Compra Ágil Chile",
    page_icon="🇨🇱",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Cargar variables de entorno
load_dotenv()

# Inicializar variables de sesión para el sistema de login
if 'logueado' not in st.session_state:
    st.session_state['logueado'] = False
    st.session_state['rol'] = None
    st.session_state['username'] = None

# ==========================================
# CONEXIÓN A BASE DE DATOS
# ==========================================
# st.cache_resource evita que Streamlit abra una nueva conexión cada vez que haces clic en la página
@st.cache_resource
def conectar_db():
    try:
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME")
        )
        return conn
    except Exception as e:
        st.error(f"Error al conectar con la base de datos en Aiven: {e}")
        return None

# ==========================================
# FUNCIONES DE OBTENCIÓN DE DATOS (CACHÉ)
# ==========================================
@st.cache_data(ttl=600)  # Los datos se actualizan cada 10 minutos (600 segundos) para no saturar Aiven
def cargar_compras_agiles():
    conn = conectar_db()
    if conn:
        query = "SELECT * FROM compras_agiles"
        df = pd.read_sql(query, conn)
        # Asegurar formato de fechas
        df['fecha_cierre'] = pd.to_datetime(df['fecha_cierre'])
        df['fecha_publicacion'] = pd.to_datetime(df['fecha_publicacion'])
        return df
    return pd.DataFrame()

@st.cache_data(ttl=3600) # Una vez por hora para los datos históricos
def cargar_proveedores_historicos():
    conn = conectar_db()
    if conn:
        query = "SELECT * FROM proveedores_historicos ORDER BY monto_total_adjudicado DESC"
        df = pd.read_sql(query, conn)
        return df
    return pd.DataFrame()

# ==========================================
# SISTEMA DE AUTENTICACIÓN (LOGIN)
# ==========================================
def verificar_credenciales(username, password_ingresada):
    conn = conectar_db()
    if not conn:
        return False, None
    
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT password_hash, rol FROM usuarios WHERE username = %s", (username,))
    usuario = cursor.fetchone()
    cursor.close()
    
    if usuario:
        # Verificar la contraseña hasheada
        if bcrypt.checkpw(password_ingresada.encode('utf-8'), usuario['password_hash'].encode('utf-8')):
            return True, usuario['rol']
    return False, None

def mostrar_pantalla_login():
    st.markdown("<h1 style='text-align: center;'>🇨🇱 Radar Mercado Público: Compra Ágil</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center;'>Plataforma de inteligencia de negocios para licitaciones del estado.</p>", unsafe_allow_html=True)
    
    st.write("---")
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        st.subheader("Acceso a la Plataforma")
        
        # Pestañas para Login de Usuario o Acceso de Invitado
        tab_login, tab_invitado = st.tabs(["🔒 Iniciar Sesión", "👀 Entrar como Invitado"])
        
        with tab_login:
            with st.form("form_login"):
                usuario_input = st.text_input("Usuario")
                password_input = st.text_input("Contraseña", type="password")
                btn_login = st.form_submit_button("Ingresar", use_container_width=True)
                
                if btn_login:
                    valido, rol = verificar_credenciales(usuario_input, password_input)
                    if valido:
                        st.session_state['logueado'] = True
                        st.session_state['rol'] = rol
                        st.session_state['username'] = usuario_input
                        st.rerun()
                    else:
                        st.error("❌ Usuario o contraseña incorrectos.")
                        st.info("Tip: Prueba con el usuario 'usuario_demo' y clave 'password123'")
                        
        with tab_invitado:
            st.write("El acceso de invitado te permite ver las oportunidades actuales, pero con funciones limitadas (sin filtros avanzados ni análisis de competencia).")
            if st.button("Ingresar como Invitado", use_container_width=True):
                st.session_state['logueado'] = True
                st.session_state['rol'] = 'invitado'
                st.session_state['username'] = 'Invitado'
                st.rerun()

# ==========================================
# DASHBOARD PRINCIPAL
# ==========================================
def mostrar_dashboard():
    # BARRA LATERAL (SIDEBAR)
    st.sidebar.image("https://upload.wikimedia.org/wikipedia/commons/thumb/e/e0/Coat_of_arms_of_Chile.svg/200px-Coat_of_arms_of_Chile.svg.png", width=100)
    st.sidebar.title("Menú de Navegación")
    
    st.sidebar.write(f"👤 **Usuario:** {st.session_state['username']}")
    st.sidebar.write(f"🏷️ **Rol:** {st.session_state['rol'].capitalize()}")
    st.sidebar.write("---")
    
    opcion_menu = st.sidebar.radio("Ir a:", ["📈 Oportunidades Activas", "🕵️ Análisis de Competencia"])
    
    if st.sidebar.button("🚪 Cerrar Sesión"):
        st.session_state['logueado'] = False
        st.session_state['rol'] = None
        st.session_state['username'] = None
        st.rerun()

    # Cargar los datos a usar
    df_compras = cargar_compras_agiles()
    
    if df_compras.empty:
        st.warning("⚠️ No hay datos en la base de datos de compras ágiles. Asegúrate de ejecutar el script `extractor.py` primero.")
        return

    # ----------------------------------------------------
    # VISTA 1: OPORTUNIDADES ACTIVAS
    # ----------------------------------------------------
    if opcion_menu == "📈 Oportunidades Activas":
        st.header("Licitaciones y Compras Ágiles Disponibles")
        
        # FILTROS
        with st.expander("🔍 Filtros de Búsqueda", expanded=True):
            col_f1, col_f2, col_f3 = st.columns(3)
            
            with col_f1:
                # Filtro básico (Todos pueden usarlo)
                lista_entidades = ["Todas"] + list(df_compras['entidad_compradora'].dropna().unique())
                entidad_seleccionada = st.selectbox("Entidad Compradora", lista_entidades)
                
            with col_f2:
                # Filtro de texto
                palabra_clave = st.text_input("Buscar por nombre de licitación")
                
            with col_f3:
                # FILTROS ESPECIALIZADOS (Solo usuarios registrados)
                if st.session_state['rol'] in ['registrado', 'admin']:
                    monto_min = st.number_input("Monto mínimo estimado ($)", min_value=0.0, value=0.0, step=10000.0)
                    st.caption("✨ Filtro avanzado habilitado")
                else:
                    monto_min = 0.0
                    st.info("🔒 Inicia sesión para filtrar por montos.")

        # Aplicar Filtros al DataFrame
        df_filtrado = df_compras.copy()
        if entidad_seleccionada != "Todas":
            df_filtrado = df_filtrado[df_filtrado['entidad_compradora'] == entidad_seleccionada]
        if palabra_clave:
            df_filtrado = df_filtrado[df_filtrado['nombre_licitacion'].str.contains(palabra_clave, case=False, na=False)]
        if monto_min > 0:
            df_filtrado = df_filtrado[df_filtrado['monto_estimado'] >= monto_min]

        # KPIs (Tarjetas de resumen)
        st.write("### Resumen de Oportunidades")
        kpi1, kpi2, kpi3, kpi4 = st.columns(4)
        kpi1.metric("Licitaciones Mostradas", len(df_filtrado))
        kpi2.metric("Monto Total Estimado", f"${df_filtrado['monto_estimado'].sum():,.0f}")
        kpi3.metric("Promedio por Licitación", f"${df_filtrado['monto_estimado'].mean():,.0f}" if len(df_filtrado)>0 else "$0")
        kpi4.metric("Entidades Solicitantes", df_filtrado['entidad_compradora'].nunique())

        st.write("---")

        # GRÁFICOS
        st.write("### Análisis Visual de Demanda")
        graf1, graf2 = st.columns(2)
        
        with graf1:
            # Gráfico: ¿Qué días se licita más? (Basado en la fecha de cierre)
            df_filtrado['dia_cierre'] = df_filtrado['fecha_cierre'].dt.date
            conteo_dias = df_filtrado.groupby('dia_cierre').size().reset_index(name='Cantidad')
            
            fig_dias = px.line(conteo_dias, x='dia_cierre', y='Cantidad', 
                               title='Volumen de Licitaciones por Fecha de Cierre',
                               markers=True, line_shape='spline', template='plotly_white')
            fig_dias.update_xaxes(title="Fecha")
            fig_dias.update_yaxes(title="N° de Licitaciones")
            st.plotly_chart(fig_dias, use_container_width=True)

        with graf2:
            # Gráfico: ¿Qué entidades solicitan más?
            conteo_entidad = df_filtrado['entidad_compradora'].value_counts().head(10).reset_index()
            conteo_entidad.columns = ['Entidad', 'Cantidad']
            
            fig_entidad = px.bar(conteo_entidad, x='Cantidad', y='Entidad', 
                                 title='Top 10 Entidades con más Compras Ágiles',
                                 orientation='h', color='Cantidad', template='plotly_white')
            fig_entidad.update_layout(yaxis={'categoryorder':'total ascending'})
            st.plotly_chart(fig_entidad, use_container_width=True)

        # TABLA DE DATOS
        st.write("### Detalle de Licitaciones")
        # Formatear montos y fechas para que se vean bien en la tabla
        df_mostrar = df_filtrado.copy()
        df_mostrar['monto_estimado'] = df_mostrar['monto_estimado'].apply(lambda x: f"${x:,.0f}")
        df_mostrar['fecha_cierre'] = df_mostrar['fecha_cierre'].dt.strftime('%d-%m-%Y %H:%M')
        
        st.dataframe(
            df_mostrar[['codigo', 'nombre_licitacion', 'entidad_compradora', 'monto_estimado', 'fecha_cierre', 'estado']],
            use_container_width=True,
            hide_index=True
        )

    # ----------------------------------------------------
    # VISTA 2: ANÁLISIS DE COMPETENCIA
    # ----------------------------------------------------
    elif opcion_menu == "🕵️ Análisis de Competencia":
        st.header("Análisis de Proveedores (Tu Competencia)")
        
        # Bloquear vista para invitados
        if st.session_state['rol'] == 'invitado':
            st.warning("🔒 Esta sección es exclusiva para usuarios registrados.")
            st.write("Aquí podrás investigar qué empresas ganan generalmente las compras ágiles, ver sus montos adjudicados y analizar su comportamiento para mejorar tu estrategia comercial.")
            st.image("https://images.unsplash.com/photo-1460925895917-afdab827c52f?ixlib=rb-4.0.3&auto=format&fit=crop&w=1200&q=80", use_column_width=True)
            return

        df_prov = cargar_proveedores_historicos()
        
        if df_prov.empty:
            st.info("No hay datos históricos de proveedores aún. El sistema se irá alimentando con el tiempo.")
            return
            
        st.write("Investiga quiénes dominan el mercado y cuáles son los montos promedio que se adjudican.")
        
        c1, c2 = st.columns(2)
        
        with c1:
            st.write("#### Relación: Adjudicaciones vs Montos Ganados")
            fig_scatter = px.scatter(df_prov, x='cantidad_adjudicaciones', y='monto_total_adjudicado',
                                     hover_name='razon_social', size='cantidad_adjudicaciones',
                                     color='monto_total_adjudicado', color_continuous_scale='Viridis',
                                     title="Mapa de Competidores (Burbujas)",
                                     labels={'cantidad_adjudicaciones': 'N° de Adjudicaciones', 
                                             'monto_total_adjudicado': 'Monto Total Ganado ($)'})
            st.plotly_chart(fig_scatter, use_container_width=True)
            
        with c2:
            st.write("#### Top Competidores por Dinero Adjudicado")
            top_prov = df_prov.head(10)
            fig_bar_prov = px.bar(top_prov, x='razon_social', y='monto_total_adjudicado',
                                  text_auto='.2s', title="Top 10 Empresas con más ingresos",
                                  labels={'razon_social': 'Empresa', 'monto_total_adjudicado': 'Monto ($)'})
            fig_bar_prov.update_traces(textfont_size=12, textangle=0, textposition="outside", cliponaxis=False)
            st.plotly_chart(fig_bar_prov, use_container_width=True)

        st.write("#### Base de Datos de Competidores")
        st.dataframe(df_prov, use_container_width=True, hide_index=True)


# ==========================================
# RUTEO PRINCIPAL
# ==========================================
if not st.session_state['logueado']:
    mostrar_pantalla_login()
else:
    mostrar_dashboard()